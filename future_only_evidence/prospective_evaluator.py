"""Canonical evidence-loading and rank-efficacy calculations.

Transportation domain evaluators compose these primitives so sleeves receive
independent verdicts without changing registered portfolio construction.
"""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .outcome_integrity_v3 import validate_and_recompute_outcomes_v3
from .prospective_contracts import (
    ProspectiveContract,
    read_calendar_bytes,
    read_json_snapshot,
    read_source_snapshots,
    scheduled_asofs,
    validate_capture_registry,
    validate_strict_capture,
)
from .official_calendar import validate_official_xnys_calendar_bytes
from .canonical_values import exact_utc
from .trusted_receipts import PinnedEd25519Authority


def _utc(value: Any, *, label: str) -> datetime:
    return exact_utc(value, label=label)


def load_verified_evidence(
    *,
    contract: ProspectiveContract,
    authority: PinnedEd25519Authority,
    capture_paths: Sequence[Path],
    capture_registry_path: Path,
    capture_registry_receipt_path: Path,
    expected_capture_registry_receipt_sha256: str,
    outcome_path: Path,
    outcome_source_paths: Mapping[str, Path],
    outcome_receipt_path: Path,
    expected_outcome_receipt_sha256: str,
    trading_calendar_path: Path,
    evaluated_at_utc: str,
) -> dict[str, Any]:
    if not capture_paths:
        raise ValueError("canonical evaluation requires at least one prospective capture")
    calendar_bytes = Path(trading_calendar_path).expanduser().resolve().read_bytes()
    validate_official_xnys_calendar_bytes(calendar_bytes)
    capture_snapshots = [
        read_json_snapshot(path, label="prospective capture") for path in capture_paths
    ]
    capture_source_snapshots_by_id: dict[str, dict[str, bytes]] = {}
    capture_receipt_snapshots_by_id: dict[str, bytes] = {}
    captures: list[dict[str, Any]] = []
    for payload, _, _, _ in capture_snapshots:
        capture_id = str(payload.get("capture_id") or "")
        identities = payload.get("source_identities")
        receipt_identity = payload.get("trusted_receipt")
        if (
            not capture_id
            or capture_id in capture_source_snapshots_by_id
            or not isinstance(identities, dict)
            or not isinstance(receipt_identity, dict)
        ):
            raise ValueError("capture lacks unique source/receipt identity")
        source_paths = {
            str(role): Path(str(identity.get("path") or ""))
            for role, identity in identities.items()
            if isinstance(identity, dict)
        }
        if set(source_paths) != set(identities):
            raise ValueError("capture contains an invalid source identity")
        source_snapshots = read_source_snapshots(source_paths)
        receipt_bytes = Path(
            str(receipt_identity.get("path") or "")
        ).expanduser().resolve().read_bytes()
        captures.append(
            validate_strict_capture(
                payload,
                contract=contract,
                authority=authority,
                trading_calendar_path=trading_calendar_path,
                source_snapshot_bytes=source_snapshots,
                trading_calendar_snapshot_bytes=calendar_bytes,
                capture_receipt_snapshot_bytes=receipt_bytes,
            )
        )
        capture_source_snapshots_by_id[capture_id] = source_snapshots
        capture_receipt_snapshots_by_id[capture_id] = receipt_bytes
    registry_bytes = Path(capture_registry_path).expanduser().resolve().read_bytes()
    registry_receipt_bytes = (
        Path(capture_registry_receipt_path).expanduser().resolve().read_bytes()
    )
    registry_audit = validate_capture_registry(
        registry_path=capture_registry_path,
        registry_receipt_path=capture_registry_receipt_path,
        expected_registry_receipt_sha256=expected_capture_registry_receipt_sha256,
        authority=authority,
        contract=contract,
        capture_paths=capture_paths,
        trading_calendar_path=trading_calendar_path,
        capture_snapshots=capture_snapshots,
        trading_calendar_snapshot_bytes=calendar_bytes,
        registry_snapshot_bytes=registry_bytes,
        registry_receipt_snapshot_bytes=registry_receipt_bytes,
    )
    evaluated_capture_hashes = {
        str(capture["capture_id"]): digest
        for capture, (_, digest, _, _) in zip(captures, capture_snapshots)
    }
    if evaluated_capture_hashes != registry_audit["capture_sha256_by_id"]:
        raise ValueError(
            "semantically evaluated capture bytes differ from the signed registry census"
        )
    outcome_source_snapshots = read_source_snapshots(outcome_source_paths)
    outcome_receipt_bytes = Path(outcome_receipt_path).expanduser().resolve().read_bytes()
    outcome_audit = validate_and_recompute_outcomes_v3(
        contract=contract,
        captures=captures,
        outcome_path=outcome_path,
        outcome_source_paths=outcome_source_paths,
        outcome_receipt_path=outcome_receipt_path,
        expected_outcome_receipt_sha256=expected_outcome_receipt_sha256,
        authority=authority,
        trading_calendar_path=trading_calendar_path,
        evaluated_at_utc=evaluated_at_utc,
        outcome_source_snapshot_bytes=outcome_source_snapshots,
        trading_calendar_snapshot_bytes=calendar_bytes,
        outcome_receipt_snapshot_bytes=outcome_receipt_bytes,
    )
    calendar_rows, session_index = read_calendar_bytes(calendar_bytes)
    anchored_at = _utc(
        outcome_audit["outcome_receipt_anchored_at_utc"],
        label="outcome receipt anchor",
    )
    scheduled = scheduled_asofs(
        contract,
        calendar_rows=calendar_rows,
        complete_through_asof=anchored_at.date().isoformat(),
    )
    due_asofs: list[str] = []
    for asof in scheduled:
        index = session_index.get(asof)
        if index is None or index + 1 >= len(calendar_rows):
            continue
        next_entry = _utc(
            calendar_rows[index + 1]["entry_execution_at_utc"],
            label="scheduled capture next-session entry",
        )
        if next_entry <= anchored_at:
            due_asofs.append(asof)
    observed_asofs = {str(capture["asof_date"]) for capture in captures}
    missing_due = sorted(set(due_asofs) - observed_asofs)
    if missing_due:
        raise ValueError(
            "signed capture registry omitted scheduled captures already due by "
            f"the outcome anchor: {missing_due}"
        )
    expected: set[tuple[str, str, int]] = set()
    matured_capture_horizons: list[dict[str, Any]] = []
    for capture in captures:
        entry_index = int(capture["trusted_capture_timing"]["entry_session_index"])
        for horizon in contract.horizons:
            exit_index = entry_index + horizon
            matured = (
                exit_index < len(calendar_rows)
                and _utc(
                    calendar_rows[exit_index]["entry_execution_at_utc"],
                    label="calendar horizon open",
                )
                < anchored_at
            )
            matured_capture_horizons.append(
                {
                    "capture_id": capture["capture_id"],
                    "asof_date": capture["asof_date"],
                    "horizon_sessions": horizon,
                    "matured_at_receipt_anchor": matured,
                    "exit_session_date": (
                        calendar_rows[exit_index]["session_date"] if exit_index < len(calendar_rows) else None
                    ),
                }
            )
            if matured:
                for signal in capture["signal_rows"]:
                    if int(signal["eligible_flag"]) == 1:
                        expected.add((capture["capture_id"], signal["ticker"], horizon))
    rows = list(outcome_audit["normalized_rows"])
    actual = {
        (str(row["capture_id"]), str(row["ticker"]), int(row["horizon_sessions"]))
        for row in rows
    }
    if len(actual) != len(rows):
        raise ValueError("normalized outcome identity is not unique")
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(
            "outcome package is not the exact all-matured capture census; "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    if not expected:
        raise ValueError("no registered prospective interval has matured")
    capture_index = {str(capture["capture_id"]): capture for capture in captures}
    return {
        "contract": contract,
        "captures": captures,
        "capture_index": capture_index,
        "outcomes": rows,
        "calendar_rows": calendar_rows,
        "session_index": session_index,
        "capture_registry_audit": registry_audit,
        "_capture_source_snapshot_bytes_by_id": capture_source_snapshots_by_id,
        "_capture_receipt_snapshot_bytes_by_id": capture_receipt_snapshots_by_id,
        "_outcome_source_snapshot_bytes": outcome_source_snapshots,
        "_outcome_receipt_snapshot_bytes": outcome_receipt_bytes,
        "_capture_registry_snapshot_bytes": registry_bytes,
        "_capture_registry_receipt_snapshot_bytes": registry_receipt_bytes,
        "_trading_calendar_snapshot_bytes": calendar_bytes,
        "due_capture_census_audit": {
            "due_asof_dates": due_asofs,
            "observed_asof_dates": sorted(observed_asofs),
            "no_due_capture_omissions_pass": True,
        },
        "outcome_integrity_audit": {
            key: value for key, value in outcome_audit.items() if key != "normalized_rows"
        },
        "matured_capture_horizons": matured_capture_horizons,
        "exact_all_matured_outcome_census_pass": True,
    }


def _average_ranks(values: Sequence[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        end = position + 1
        while end < len(indexed) and indexed[end][1] == indexed[position][1]:
            end += 1
        average = (position + 1 + end) / 2.0
        for original, _ in indexed[position:end]:
            ranks[original] = average
        position = end
    return ranks


def _correlation(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    if denominator == 0:
        return None
    return numerator / denominator


def _spearman(scores: Sequence[float], returns: Sequence[float]) -> float | None:
    return _correlation(_average_ranks(scores), _average_ranks(returns))


def _turnover(previous: set[str] | None, current: set[str]) -> float:
    if not current:
        raise ValueError("selected portfolio cannot be empty")
    if previous is None:
        return 1.0
    union = previous | current
    prior_weight = 1.0 / len(previous) if previous else 0.0
    current_weight = 1.0 / len(current)
    return 0.5 * sum(
        abs(
            (prior_weight if ticker in previous else 0.0)
            - (current_weight if ticker in current else 0.0)
        )
        for ticker in union
    )


def build_ranked_periods(
    evidence: Mapping[str, Any],
    *,
    sleeve_id: str,
    group_id: str | None,
    horizon: int,
) -> list[dict[str, Any]]:
    outcomes = list(evidence["outcomes"])
    periods: list[dict[str, Any]] = []
    for capture in evidence["captures"]:
        signals = [
            row
            for row in capture["signal_rows"]
            if row["sleeve_id"] == sleeve_id
            and (group_id is None or row["group_id"] == group_id)
            and int(row.get("predictive_eligible_flag", row["eligible_flag"])) == 1
        ]
        if not signals:
            continue
        rows = [
            row
            for row in outcomes
            if row["capture_id"] == capture["capture_id"]
            and row["sleeve_id"] == sleeve_id
            and (group_id is None or row["group_id"] == group_id)
            and int(row["horizon_sessions"]) == horizon
            and any(signal["ticker"] == row["ticker"] for signal in signals)
        ]
        if not rows:
            continue
        returns = {row["ticker"]: float(row["gross_return"]) for row in rows}
        expected = {row["ticker"] for row in signals}
        if set(returns) != expected:
            raise ValueError("ranked scope outcome census differs from predictive signal census")
        top = {row["ticker"] for row in signals if int(row["selected_top_flag"]) == 1}
        bottom = {row["ticker"] for row in signals if int(row["selected_bottom_flag"]) == 1}
        if not top or not bottom:
            raise ValueError("ranked scope needs non-empty frozen top and bottom selections")
        entry_dates = {str(row["entry_date"]) for row in rows}
        exit_dates = {str(row["exit_date"]) for row in rows}
        if len(entry_dates) != 1 or len(exit_dates) != 1:
            raise ValueError("one capture/horizon does not share one exact interval")
        scores = [float(row["score"]) for row in signals]
        realized = [returns[row["ticker"]] for row in signals]
        periods.append(
            {
                "capture_id": capture["capture_id"],
                "asof_date": capture["asof_date"],
                "entry_date": next(iter(entry_dates)),
                "exit_date": next(iter(exit_dates)),
                "entry_session_index": int(rows[0]["entry_session_index"]),
                "exit_session_index": int(rows[0]["exit_session_index"]),
                "cross_section": len(signals),
                "ic": _spearman(scores, realized),
                "top_gross": statistics.fmean(returns[ticker] for ticker in top),
                "bottom_gross": statistics.fmean(returns[ticker] for ticker in bottom),
                "cohort_gross": statistics.fmean(returns.values()),
                "top_tickers": sorted(top),
                "bottom_tickers": sorted(bottom),
                "cohort_tickers": sorted(returns),
            }
        )
    return sorted(periods, key=lambda row: (row["entry_session_index"], row["capture_id"]))


def deterministic_nonoverlap(periods: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    last_exit: int | None = None
    for raw in sorted(periods, key=lambda row: (int(row["entry_session_index"]), str(row["capture_id"]))):
        row = dict(raw)
        entry = int(row["entry_session_index"])
        exit_index = int(row["exit_session_index"])
        if exit_index <= entry:
            raise ValueError("outcome interval is non-positive")
        if last_exit is None or entry >= last_exit:
            selected.append(row)
            last_exit = exit_index
    return selected


def apply_costs(
    periods: Sequence[Mapping[str, Any]],
    *,
    transaction_cost_bps: float,
) -> list[dict[str, Any]]:
    rate = transaction_cost_bps / 10_000.0
    result: list[dict[str, Any]] = []
    previous: dict[str, set[str]] | None = None
    previous_exit: int | None = None
    for raw in periods:
        row = dict(raw)
        entry = int(row["entry_session_index"])
        gap = previous_exit is not None and entry > previous_exit
        if gap and result:
            for leg in ("top", "bottom", "cohort"):
                result[-1][f"{leg}_exit_turnover"] = 1.0
        if previous is None or gap:
            top_entry = bottom_entry = cohort_entry = 1.0
        else:
            top_entry = _turnover(previous["top"], set(row["top_tickers"]))
            bottom_entry = _turnover(previous["bottom"], set(row["bottom_tickers"]))
            cohort_entry = _turnover(previous["cohort"], set(row["cohort_tickers"]))
        row.update(
            top_entry_turnover=top_entry,
            bottom_entry_turnover=bottom_entry,
            cohort_entry_turnover=cohort_entry,
            top_exit_turnover=0.0,
            bottom_exit_turnover=0.0,
            cohort_exit_turnover=0.0,
        )
        result.append(row)
        previous = {
            "top": set(row["top_tickers"]),
            "bottom": set(row["bottom_tickers"]),
            "cohort": set(row["cohort_tickers"]),
        }
        previous_exit = int(row["exit_session_index"])
    if result:
        for leg in ("top", "bottom", "cohort"):
            result[-1][f"{leg}_exit_turnover"] = 1.0
    for row in result:
        top_turnover = row["top_entry_turnover"] + row["top_exit_turnover"]
        bottom_turnover = row["bottom_entry_turnover"] + row["bottom_exit_turnover"]
        cohort_turnover = row["cohort_entry_turnover"] + row["cohort_exit_turnover"]
        row["top_net"] = row["top_gross"] - rate * top_turnover
        row["bottom_net_long_only"] = row["bottom_gross"] - rate * bottom_turnover
        row["cohort_net_monitor"] = row["cohort_gross"] - rate * cohort_turnover
        row["top_minus_cohort_net"] = (
            row["top_gross"]
            - row["cohort_gross"]
            - rate * (top_turnover + cohort_turnover)
        )
        row["top_minus_bottom_gross"] = row["top_gross"] - row["bottom_gross"]
        row["top_minus_bottom_net"] = (
            row["top_gross"]
            - row["bottom_gross"]
            - rate * (top_turnover + bottom_turnover)
        )
    return result


def scope_verdict(
    periods: Sequence[Mapping[str, Any]],
    *,
    contract: ProspectiveContract,
    horizon: int,
    minimum_cross_section: int,
    efficacy_field: str,
    hit_field: str,
) -> dict[str, Any]:
    rows = [dict(row) for row in periods]
    count = len(rows)
    ics = [row.get("ic") for row in rows]
    all_ic_defined = bool(rows) and all(value is not None and math.isfinite(float(value)) for value in ics)
    mean_ic = statistics.fmean(float(value) for value in ics) if all_ic_defined else None
    efficacy_values = [float(row[efficacy_field]) for row in rows]
    mean_efficacy = statistics.fmean(efficacy_values) if efficacy_values else None
    spread_field = (
        "top_minus_bottom_gross"
        if contract.top_minus_bottom_basis == "gross"
        else "top_minus_bottom_net"
    )
    spreads = [float(row[spread_field]) for row in rows]
    mean_spread = statistics.fmean(spreads) if spreads else None
    hits = [1.0 if float(row[hit_field]) > 0.0 else 0.0 for row in rows]
    hit_rate = statistics.fmean(hits) if hits else None
    positive_ic_count = sum(float(value) > 0.0 for value in ics) if all_ic_defined else 0
    ic_sign_pvalue = (
        sum(
            math.comb(count, successes) * (0.5**count)
            for successes in range(positive_ic_count, count + 1)
        )
        if all_ic_defined and count
        else None
    )
    gates = {
        "minimum_count_pass": count >= int(contract.minimum_counts[horizon]),
        "all_counted_ic_defined_pass": all_ic_defined,
        "mean_ic_pass": mean_ic is not None and mean_ic > contract.minimum_ic,
        "ic_sign_test_pass": ic_sign_pvalue is not None
        and ic_sign_pvalue <= contract.maximum_ic_sign_pvalue,
        "efficacy_pass": mean_efficacy is not None and mean_efficacy > contract.minimum_efficacy,
        "top_minus_bottom_pass": mean_spread is not None and mean_spread > contract.minimum_top_minus_bottom,
        "hit_rate_pass": hit_rate is not None and hit_rate >= contract.minimum_hit_rate,
        "cross_section_pass": bool(rows)
        and all(int(row["cross_section"]) >= minimum_cross_section for row in rows),
        "initial_cost_charged_pass": bool(rows)
        and float(rows[0]["top_entry_turnover"]) == 1.0,
        "final_cost_charged_pass": bool(rows)
        and float(rows[-1]["top_exit_turnover"]) == 1.0,
        "true_nonoverlap_pass": all(
            int(left["exit_session_index"]) <= int(right["entry_session_index"])
            for left, right in zip(rows, rows[1:])
        ),
    }
    return {
        "horizon_sessions": horizon,
        "nonoverlapping_outcome_count": count,
        "minimum_required_count": int(contract.minimum_counts[horizon]),
        "remaining_count": max(0, int(contract.minimum_counts[horizon]) - count),
        "mean_ic": mean_ic,
        "positive_ic_count": positive_ic_count,
        "one_sided_ic_sign_pvalue": ic_sign_pvalue,
        "maximum_ic_sign_pvalue": contract.maximum_ic_sign_pvalue,
        "efficacy_metric": efficacy_field,
        "mean_efficacy": mean_efficacy,
        "mean_top_minus_cohort_net": (
            statistics.fmean(float(row["top_minus_cohort_net"]) for row in rows)
            if rows
            else None
        ),
        "mean_top_residual_net": (
            statistics.fmean(float(row["top_net"]) for row in rows) if rows else None
        ),
        "top_minus_bottom_gate_basis": contract.top_minus_bottom_basis,
        "mean_top_minus_bottom": mean_spread,
        "hit_metric": hit_field,
        "hit_rate": hit_rate,
        "transaction_cost_bps": contract.transaction_cost_bps,
        "gates": gates,
        "pass": all(gates.values()),
        "periods": rows,
    }


def weighted_verdict_periods(
    group_periods: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    group_weights: Mapping[str, float],
) -> list[dict[str, Any]]:
    if set(group_periods) != set(group_weights):
        raise ValueError("weighted aggregate group census differs from frozen weights")
    indexes = {
        group: {str(row["capture_id"]): dict(row) for row in rows}
        for group, rows in group_periods.items()
    }
    capture_sets = [set(index) for index in indexes.values()]
    if not capture_sets or any(value != capture_sets[0] for value in capture_sets[1:]):
        raise ValueError("ranked groups do not share an exact aggregate period census")
    weight_total = sum(float(value) for value in group_weights.values())
    if not 0 < weight_total <= 1.0 + 1e-12:
        raise ValueError("frozen predictive group weights are invalid")
    result: list[dict[str, Any]] = []
    for capture_id in sorted(
        capture_sets[0],
        key=lambda value: int(next(iter(indexes.values()))[value]["entry_session_index"]),
    ):
        selected = {group: indexes[group][capture_id] for group in group_weights}
        first = next(iter(selected.values()))
        if any(
            (row["entry_session_index"], row["exit_session_index"])
            != (first["entry_session_index"], first["exit_session_index"])
            for row in selected.values()
        ):
            raise ValueError("weighted groups do not share one exact interval")
        if any(row.get("ic") is None for row in selected.values()):
            weighted_ic = None
        else:
            weighted_ic = sum(
                float(group_weights[group]) * float(row["ic"])
                for group, row in selected.items()
            ) / weight_total
        result.append(
            {
                "capture_id": capture_id,
                "asof_date": first["asof_date"],
                "entry_date": first["entry_date"],
                "exit_date": first["exit_date"],
                "entry_session_index": first["entry_session_index"],
                "exit_session_index": first["exit_session_index"],
                "cross_section": sum(int(row["cross_section"]) for row in selected.values()),
                "ic": weighted_ic,
                "top_gross": sum(
                    float(group_weights[group]) * float(row["top_gross"])
                    for group, row in selected.items()
                ) / weight_total,
                "bottom_gross": sum(
                    float(group_weights[group]) * float(row["bottom_gross"])
                    for group, row in selected.items()
                ) / weight_total,
                "cohort_gross": sum(
                    float(group_weights[group]) * float(row["cohort_gross"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_net": sum(
                    float(group_weights[group]) * float(row["top_net"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_minus_cohort_net": sum(
                    float(group_weights[group]) * float(row["top_minus_cohort_net"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_minus_bottom_gross": sum(
                    float(group_weights[group]) * float(row["top_minus_bottom_gross"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_minus_bottom_net": sum(
                    float(group_weights[group]) * float(row["top_minus_bottom_net"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_entry_turnover": sum(
                    float(group_weights[group]) * float(row["top_entry_turnover"])
                    for group, row in selected.items()
                ) / weight_total,
                "top_exit_turnover": sum(
                    float(group_weights[group]) * float(row["top_exit_turnover"])
                    for group, row in selected.items()
                ) / weight_total,
                "group_weight_total": weight_total,
                "group_contributions": {
                    group: {
                        "weight": float(group_weights[group]),
                        "top_gross": row["top_gross"],
                        "top_net": row["top_net"],
                        "top_minus_cohort_net": row["top_minus_cohort_net"],
                    }
                    for group, row in sorted(selected.items())
                },
            }
        )
    return result


def equal_weight_monitor_periods(
    evidence: Mapping[str, Any],
    *,
    sleeve_id: str,
    group_id: str,
    horizon: int,
    transaction_cost_bps: float,
) -> list[dict[str, Any]]:
    periods: list[dict[str, Any]] = []
    outcomes = list(evidence["outcomes"])
    for capture in evidence["captures"]:
        signals = [
            row
            for row in capture["signal_rows"]
            if row["sleeve_id"] == sleeve_id
            and row["group_id"] == group_id
            and int(row["eligible_flag"]) == 1
        ]
        if not signals:
            continue
        rows = [
            row
            for row in outcomes
            if row["capture_id"] == capture["capture_id"]
            and row["sleeve_id"] == sleeve_id
            and row["group_id"] == group_id
            and int(row["horizon_sessions"]) == horizon
        ]
        if not rows:
            continue
        if {row["ticker"] for row in rows} != {row["ticker"] for row in signals}:
            raise ValueError("equal-weight monitor outcome census differs from capture")
        tickers = sorted(row["ticker"] for row in rows)
        gross = statistics.fmean(float(row["gross_return"]) for row in rows)
        periods.append(
            {
                "capture_id": capture["capture_id"],
                "asof_date": capture["asof_date"],
                "entry_date": rows[0]["entry_date"],
                "exit_date": rows[0]["exit_date"],
                "entry_session_index": int(rows[0]["entry_session_index"]),
                "exit_session_index": int(rows[0]["exit_session_index"]),
                "member_tickers": tickers,
                "gross_return": gross,
            }
        )
    selected = deterministic_nonoverlap(periods)
    rate = transaction_cost_bps / 10_000.0
    previous: set[str] | None = None
    previous_exit: int | None = None
    result: list[dict[str, Any]] = []
    for row in selected:
        current = set(row["member_tickers"])
        gap = previous_exit is not None and int(row["entry_session_index"]) > previous_exit
        if gap and result:
            result[-1]["exit_turnover"] = 1.0
        entry_turnover = 1.0 if previous is None or gap else _turnover(previous, current)
        result.append({**row, "entry_turnover": entry_turnover, "exit_turnover": 0.0})
        previous = current
        previous_exit = int(row["exit_session_index"])
    if result:
        result[-1]["exit_turnover"] = 1.0
    for row in result:
        row["net_return"] = row["gross_return"] - rate * (
            row["entry_turnover"] + row["exit_turnover"]
        )
    return result


__all__ = [
    "apply_costs",
    "build_ranked_periods",
    "deterministic_nonoverlap",
    "equal_weight_monitor_periods",
    "load_verified_evidence",
    "scope_verdict",
    "weighted_verdict_periods",
]
