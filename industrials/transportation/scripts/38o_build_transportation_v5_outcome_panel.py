#!/usr/bin/env python3
"""Build the v5 diagnostic outcome panel from the pinned raw price slice."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect  # noqa: E402
from industrials.core.oos_price_lineage import (  # noqa: E402
    PRICE_SLICE_FIELDS,
    price_slice_rows,
    prices_from_slice,
)
from industrials.core.oos_research import (  # noqa: E402
    ExecutionPricePoint,
    execution_window,
    finite_float,
    fmt,
    parse_date,
)
from industrials.core.reports import write_csv_atomic, write_text_atomic  # noqa: E402
from industrials.transportation.contracts import COMPONENT_FIELDS, file_sha256  # noqa: E402
from industrials.transportation.oos_identity import (  # noqa: E402
    load_aliases,
    load_continuity,
    load_memberships,
    resolve_price_ticker,
)
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation"
DEFAULT_CONTRACT = ROOT / "investable_v5" / "prebuild_contract" / "2026-08-15" / "transportation_v5_prebuild_contract.json"
DEFAULT_SCORE_ROOT = ROOT / "investable_v5" / "pit_score_history" / "2026-08-15"
DEFAULT_SCORE_VALIDATION = ROOT / "investable_v5" / "pit_score_validation" / "2026-08-15" / "transportation_v5_pit_score_history_validation.json"
DEFAULT_PROTOCOL = ROOT / "investable_v5" / "research_protocol" / "2026-08-15" / "transportation_v5_research_protocol.json"
DEFAULT_OUTPUT_DIR = ROOT / "investable_v5" / "outcome_panel" / "2026-08-15"
PANEL_FIELDS = (
    "asof_date",
    "ticker",
    "company_name",
    "industry",
    "calibration_cohort",
    "calibration_use",
    "development_stage",
    "calibration_pool",
    "economic_peer_group",
    "risk_tier",
    "portfolio_role",
    "membership_source_id",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "historical_calibration_only_flag",
    "current_portfolio_eligibility_authorized",
    "terminal_type",
    "physical_price_ticker",
    "alias_resolution",
    "metric_registry_version",
    "metric_values_json",
    "metric_status_json",
    *COMPONENT_FIELDS,
    "baseline_final_score",
    "rank_ready_flag",
    "calibration_eligible_flag",
    "calibration_eligible_reason",
    "horizon_sessions",
    "security_price_source_id",
    "entry_date",
    "entry_adjusted_open",
    "exit_date",
    "exit_execution_value",
    "outcome_method",
    "security_forward_return",
    "benchmark_ticker",
    "benchmark_price_source_id",
    "benchmark_entry_date",
    "benchmark_exit_date",
    "benchmark_forward_return",
    "forward_excess_return",
    "outcome_available_flag",
    "outcome_unavailable_reason",
    "source_score_sha256",
    "source_calibration_sidecar_sha256",
    "source_snapshot_manifest_sha256",
)
SOURCE_FIELDS = (
    "asof_date",
    "score_path",
    "score_sha256",
    "calibration_sidecar_path",
    "calibration_sidecar_sha256",
    "manifest_path",
    "manifest_sha256",
    "row_count",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    parser.add_argument("--score-root", type=Path, default=DEFAULT_SCORE_ROOT)
    parser.add_argument("--score-validation", type=Path, default=DEFAULT_SCORE_VALIDATION)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path}: expected JSON object")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def normalized_price_points(
    rows: list[dict[str, str]],
) -> dict[str, dict[str, list[ExecutionPricePoint]]]:
    output: dict[str, dict[str, list[ExecutionPricePoint]]] = {}
    keys: set[tuple[str, str, date]] = set()
    for row in rows:
        ticker = str(row.get("ticker") or "").upper()
        source = str(row.get("source_id") or "")
        bar_date = parse_date(row.get("bar_date"), field="bar_date")
        key = (ticker, source, bar_date)
        if not ticker or not source or key in keys:
            raise ValueError(f"invalid or duplicate pinned raw price key={key}")
        keys.add(key)
        close = finite_float(row.get("close"))
        adjusted_close = finite_float(row.get("adj_close"))
        open_value = finite_float(row.get("open"))
        value = adjusted_close if adjusted_close is not None else close
        if value is None or value < 0:
            continue
        adjusted_open = open_value
        if (
            adjusted_close is not None
            and close is not None
            and close > 0
            and open_value is not None
            and open_value > 0
        ):
            adjusted_open = open_value * adjusted_close / close
        output.setdefault(ticker, {}).setdefault(source, []).append(
            ExecutionPricePoint(
                bar_date=bar_date,
                adjusted_close=value,
                adjusted_open=adjusted_open,
                source_id=source,
                price_basis=(
                    "split_dividend_adjusted_open"
                    if adjusted_close is not None
                    else "raw_open"
                ),
                price_adjustment=str(row.get("price_adjustment") or ""),
            )
        )
    for by_source in output.values():
        for points in by_source.values():
            points.sort(key=lambda item: item.bar_date)
    return output


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    contract_path = args.contract.expanduser().resolve()
    score_root = args.score_root.expanduser().resolve()
    validation_path = args.score_validation.expanduser().resolve()
    protocol_path = args.protocol.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    contract = read_json(contract_path)
    validation = read_json(validation_path)
    protocol = read_json(protocol_path)
    if validation.get("acceptance") != "PASS":
        raise ValueError("score-history validation is not PASS")
    if protocol.get("acceptance") != "PASS" or protocol.get("outcomes_accessed") is not False:
        raise ValueError("outcome-blind research protocol is not frozen")
    if protocol.get("score_history_validation_sha256") != file_sha256(validation_path):
        raise ValueError("research protocol does not pin score-history validation")
    scope_path = Path(str(contract["artifacts"]["bounded_rebuild_scope"]["path"]))
    raw_price_path = Path(str(contract["artifacts"]["price_slice"]["path"]))
    if file_sha256(raw_price_path) != str(contract["artifacts"]["price_slice"]["sha256"]):
        raise ValueError("pinned raw price slice hash mismatch")
    scope_rows = read_csv(scope_path)
    scope_by_ticker = {str(row["ticker"]).upper(): row for row in scope_rows}
    build = read_json(score_root / "transportation_v5_pit_score_history_build.json")
    if build.get("completion_status") != "COMPLETE":
        raise ValueError("score history is incomplete")
    dates = sorted(
        path.name
        for path in (score_root / "snapshots").iterdir()
        if path.is_dir() and len(path.name) == 10 and (path / "manifest.json").is_file()
    )
    raw_price_rows = read_csv(raw_price_path)
    prices = normalized_price_points(raw_price_rows)
    normalized_rows = price_slice_rows(
        prices,
        start_date=min(str(row["bar_date"]) for row in raw_price_rows),
        end_date=max(str(row["bar_date"]) for row in raw_price_rows),
    )
    prices = prices_from_slice(normalized_rows)
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=config_path.parent
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
    ) as connection:
        aliases = load_aliases(
            connection, source_id=str(universe["ticker_aliases_source_id"])
        )
        memberships = load_memberships(connection)
        continuity = load_continuity(connection)
    active_source = str(family["historical_load"]["active_price_source_id"])
    delisted_source = str(family["historical_load"]["delisted_price_source_id"])
    evaluation = dict(protocol["evaluation"])
    horizons = tuple(int(item) for item in evaluation["horizons_sessions"])
    benchmark = str(evaluation["benchmark_ticker"]).upper()
    panel_rows: list[dict[str, str]] = []
    source_rows: list[dict[str, Any]] = []
    identity_rows: list[dict[str, Any]] = []
    identity_seen: set[tuple[str, str]] = set()
    for asof in dates:
        snapshot_dir = score_root / "snapshots" / asof
        score_path = snapshot_dir / "scoring_features.csv"
        sidecar_path = snapshot_dir / "calibration_eligibility.csv"
        snapshot_manifest_path = snapshot_dir / "manifest.json"
        scores = read_csv(score_path)
        sidecar = {
            str(row["ticker"]).upper(): row for row in read_csv(sidecar_path)
        }
        score_sha = file_sha256(score_path)
        sidecar_sha = file_sha256(sidecar_path)
        snapshot_sha = file_sha256(snapshot_manifest_path)
        source_rows.append(
            {
                "asof_date": asof,
                "score_path": str(score_path),
                "score_sha256": score_sha,
                "calibration_sidecar_path": str(sidecar_path),
                "calibration_sidecar_sha256": sidecar_sha,
                "manifest_path": str(snapshot_manifest_path),
                "manifest_sha256": snapshot_sha,
                "row_count": len(scores),
            }
        )
        benchmark_windows = {
            horizon: execution_window(
                prices.get(benchmark, {}),
                asof=asof,
                horizon_sessions=horizon,
                source_order=[active_source, delisted_source],
            )
            for horizon in horizons
        }
        for score in scores:
            ticker = str(score["ticker"]).upper()
            scope = scope_by_ticker[ticker]
            gate = sidecar[ticker]
            membership_source = str(score["membership_source_id"])
            membership = memberships.get((ticker, membership_source), {})
            asof_date = parse_date(asof)
            physical_ticker, alias_resolution = resolve_price_ticker(
                ticker, asof=asof_date, aliases=aliases
            )
            if physical_ticker not in prices and ticker in prices:
                physical_ticker = ticker
                alias_resolution = (
                    f"{alias_resolution}|pinned_logical_price_ticker"
                    if alias_resolution
                    else "pinned_logical_price_ticker"
                )
            historical_only = str(gate["historical_calibration_only_flag"]) == "1"
            terminal_date = (
                parse_date(scope["effective_to"], field="scope effective_to")
                if historical_only
                else None
            )
            terminal_type = str(membership.get("terminal_type") or "")
            continuity_policy = continuity.get(ticker, {})
            preferred_sources = (
                [delisted_source, active_source]
                if historical_only
                else [active_source, delisted_source]
            )
            identity_key = (ticker, membership_source)
            if identity_key not in identity_seen:
                identity_seen.add(identity_key)
                identity_rows.append(
                    {
                        "ticker": ticker,
                        "membership_source_id": membership_source,
                        "effective_from": scope["effective_from"],
                        "effective_to": scope["effective_to"],
                        "historical_calibration_only_flag": int(historical_only),
                        "terminal_type": terminal_type,
                        "current_security_start_date": (
                            continuity_policy.get("current_security_start_date").isoformat()
                            if isinstance(continuity_policy.get("current_security_start_date"), date)
                            else ""
                        ),
                        "structural_break_date": (
                            continuity_policy.get("structural_break_date").isoformat()
                            if isinstance(continuity_policy.get("structural_break_date"), date)
                            else ""
                        ),
                    }
                )
            for horizon in horizons:
                benchmark_window = benchmark_windows[horizon]
                horizon_end = (
                    benchmark_window.exit.bar_date
                    if benchmark_window.exit is not None
                    else None
                )
                window = execution_window(
                    prices.get(physical_ticker, {}),
                    asof=asof,
                    horizon_sessions=horizon,
                    source_order=preferred_sources,
                    terminal_date=terminal_date,
                    terminal_type=terminal_type,
                    horizon_end=horizon_end,
                    current_security_start_date=(
                        continuity_policy.get("current_security_start_date")
                        if isinstance(continuity_policy.get("current_security_start_date"), date)
                        else None
                    ),
                    structural_break_date=(
                        continuity_policy.get("structural_break_date")
                        if isinstance(continuity_policy.get("structural_break_date"), date)
                        else None
                    ),
                )
                security_return = window.return_value
                benchmark_return = benchmark_window.return_value
                excess = (
                    security_return - benchmark_return
                    if security_return is not None and benchmark_return is not None
                    else None
                )
                exit_value = (
                    window.exit.adjusted_close
                    if window.exit is not None and window.terminal_exit
                    else window.exit.adjusted_open
                    if window.exit is not None
                    else None
                )
                row = {
                    field: str(score.get(field) or "")
                    for field in (
                        "asof_date",
                        "ticker",
                        "company_name",
                        "industry",
                        "calibration_cohort",
                        "calibration_use",
                        "development_stage",
                        "calibration_pool",
                        "economic_peer_group",
                        "risk_tier",
                        "portfolio_role",
                        "membership_source_id",
                        "membership_start_date",
                        "membership_end_date",
                        "membership_status",
                        "metric_registry_version",
                        "metric_values_json",
                        "metric_status_json",
                        *COMPONENT_FIELDS,
                    )
                }
                # The scoring contract retains the database calibration pool
                # for backward compatibility; all research/governance
                # artifacts use the policy cohort ID validated in the sidecar.
                row["calibration_cohort"] = str(gate.get("cohort_id") or "")
                row.update(
                    {
                        "historical_calibration_only_flag": str(int(historical_only)),
                        "current_portfolio_eligibility_authorized": "0",
                        "terminal_type": terminal_type,
                        "physical_price_ticker": physical_ticker,
                        "alias_resolution": alias_resolution,
                        "baseline_final_score": score.get("final_score", ""),
                        "rank_ready_flag": score.get("rank_ready_flag", ""),
                        "calibration_eligible_flag": gate.get("calibration_input_ready_flag", ""),
                        "calibration_eligible_reason": gate.get("calibration_input_ready_reason", ""),
                        "horizon_sessions": str(horizon),
                        "security_price_source_id": window.entry.source_id if window.entry else "",
                        "entry_date": window.entry.bar_date.isoformat() if window.entry else "",
                        "entry_adjusted_open": fmt(window.entry.adjusted_open if window.entry else None),
                        "exit_date": window.exit.bar_date.isoformat() if window.exit else "",
                        "exit_execution_value": fmt(exit_value),
                        "outcome_method": window.method,
                        "security_forward_return": fmt(security_return),
                        "benchmark_ticker": benchmark,
                        "benchmark_price_source_id": benchmark_window.entry.source_id if benchmark_window.entry else "",
                        "benchmark_entry_date": benchmark_window.entry.bar_date.isoformat() if benchmark_window.entry else "",
                        "benchmark_exit_date": benchmark_window.exit.bar_date.isoformat() if benchmark_window.exit else "",
                        "benchmark_forward_return": fmt(benchmark_return),
                        "forward_excess_return": fmt(excess),
                        "outcome_available_flag": "1" if excess is not None else "0",
                        "outcome_unavailable_reason": (
                            "" if excess is not None else window.unavailable_reason or benchmark_window.unavailable_reason
                        ),
                        "source_score_sha256": score_sha,
                        "source_calibration_sidecar_sha256": sidecar_sha,
                        "source_snapshot_manifest_sha256": snapshot_sha,
                    }
                )
                panel_rows.append({field: str(row.get(field) or "") for field in PANEL_FIELDS})
    output_dir.mkdir(parents=True, exist_ok=True)
    panel_path = output_dir / "transportation_v5_outcome_panel.csv"
    price_path = output_dir / "transportation_v5_normalized_price_slice.csv"
    source_path = output_dir / "transportation_v5_outcome_source_index.csv"
    identity_path = output_dir / "transportation_v5_outcome_identity.json"
    manifest_path = output_dir / "transportation_v5_outcome_panel_manifest.json"
    write_csv_atomic(panel_path, PANEL_FIELDS, panel_rows)
    write_csv_atomic(price_path, PRICE_SLICE_FIELDS, normalized_rows)
    write_csv_atomic(source_path, SOURCE_FIELDS, source_rows)
    write_text_atomic(
        identity_path,
        json.dumps(sorted(identity_rows, key=lambda item: (item["ticker"], item["membership_source_id"])), indent=2, sort_keys=True) + "\n",
    )
    manifest = {
        "acceptance": "PASS",
        "contract_version": "transportation_v5_diagnostic_outcome_panel_v1",
        "return_basis": "next_session_open_execution_excess",
        "benchmark_ticker": benchmark,
        "horizons_sessions": list(horizons),
        "snapshot_cadence": "month_end",
        "survivorship_corrected": True,
        "cohort_isolated": True,
        "historical_evidence_class": "diagnostic_only",
        "historical_results_can_authorize_production": False,
        "panel_path": str(panel_path),
        "panel_sha256": file_sha256(panel_path),
        "panel_row_count": len(panel_rows),
        "normalized_price_slice_path": str(price_path),
        "normalized_price_slice_sha256": file_sha256(price_path),
        "normalized_price_slice_row_count": len(normalized_rows),
        "pinned_raw_price_slice_path": str(raw_price_path),
        "pinned_raw_price_slice_sha256": file_sha256(raw_price_path),
        "source_index_path": str(source_path),
        "source_index_sha256": file_sha256(source_path),
        "identity_path": str(identity_path),
        "identity_sha256": file_sha256(identity_path),
        "score_history_validation_path": str(validation_path),
        "score_history_validation_sha256": file_sha256(validation_path),
        "research_protocol_path": str(protocol_path),
        "research_protocol_sha256": file_sha256(protocol_path),
        "snapshot_count": len(dates),
        "eligible_row_count": sum(row["calibration_eligible_flag"] == "1" for row in panel_rows),
        "outcome_available_row_count": sum(row["outcome_available_flag"] == "1" for row in panel_rows),
        "network_requests": 0,
        "parser_invocations": 0,
        "production_activation_authorized": False,
        "next_gate": "INDEPENDENTLY_RECONCILE_V5_OUTCOME_PANEL",
    }
    write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
