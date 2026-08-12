#!/usr/bin/env python3
"""Combine score baseline, structured events, and market evidence into advisory states."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    fetch_universe_snapshot,
    database_writer_lock,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    ACTION_STATES,
    INTERNAL_STATES,
    action_state_for,
    decayed_event_points,
    digest,
    ensure_state_schema,
    finite_number,
    internal_state_for,
    trading_days_between,
    utc_now,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
STATE_FIELDS = [
    "ticker", "run_as_of", "asof_ts", "tier", "is_holding", "is_target",
    "investable_eligible", "source_pipeline", "sector", "industry", "rating",
    "final_score", "score_confidence", "within_pipeline_percentile", "baseline_points",
    "company_event_points", "external_intel_points", "market_points",
    "peer_readthrough_points", "les_total", "internal_state", "action_state",
    "market_data_status",
    "prior_internal_state", "state_changed", "escalation_flags_json",
    "top_contributors_json", "input_digest",
]
TRANSITION_FIELDS = [
    "transition_id", "ticker", "transition_ts", "run_as_of", "from_state", "to_state",
    "trigger", "rule_id", "evidence_event_ids", "dwell_days_met", "approved_by", "note",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--universe-as-of", type=date.fromisoformat)
    parser.add_argument("--events-dir", type=Path)
    parser.add_argument("--signals-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _check_manifest(path: Path, *, as_of: str, accepted: set[str]) -> dict[str, Any]:
    manifest = read_manifest(path)
    if manifest.get("acceptance") not in accepted or manifest.get("as_of_date") != as_of:
        raise ValueError(f"Manifest is not accepted/current: {path}")
    for filename, expected in dict(manifest.get("outputs_sha256", {})).items():
        child = path.parent / filename
        if not child.is_file() or sha256_file(child) != expected:
            raise ValueError(f"Manifest output hash mismatch: {child}")
    return manifest


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return [dict(row) for row in reader]


def _index_unique(
    rows: list[dict[str, Any]], *, key: str, label: str
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = str(row.get(key, "")).strip()
        if not value:
            raise ValueError(f"{label} contains a blank {key}")
        if value in output:
            raise ValueError(f"{label} contains duplicate {key}={value}")
        output[value] = row
    return output


def _percentiles(universe: list[dict[str, Any]]) -> dict[str, float]:
    groups: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for row in universe:
        raw = row.get("final_score")
        if raw is None:
            continue
        value = float(raw)
        if math.isfinite(value):
            groups[str(row["source_pipeline"])].append((str(row["ticker"]), value))
    output: dict[str, float] = {}
    for values in groups.values():
        ordered = sorted(values, key=lambda item: (item[1], item[0]))
        denominator = max(1, len(ordered) - 1)
        for index, (ticker, _) in enumerate(ordered):
            output[ticker] = 100.0 * index / denominator if len(ordered) > 1 else 50.0
    return output


def _event_family(event_type: str) -> str:
    if event_type.startswith("guidance_"):
        return "guidance"
    if event_type.startswith("preannounce_"):
        return "preannounce"
    return event_type


def _replacement_family(row: dict[str, Any]) -> str:
    family = _event_family(str(row["event_type"]))
    driver = str(row.get("driver_tag", "")).strip().casefold()
    return f"{family}:{driver}" if driver else family


def _active_events(rows: list[dict[str, Any]], *, as_of: str) -> list[dict[str, Any]]:
    latest_until_replaced: dict[tuple[str, str], dict[str, Any]] = {}
    active: list[dict[str, Any]] = []
    for row in rows:
        if str(row["event_date"]) > as_of or row["review_status"] == "dismissed":
            continue
        if row["decay_mode"] == "until_replaced":
            key = (str(row["ticker"]), _replacement_family(row))
            prior = latest_until_replaced.get(key)
            if prior is None or (str(row["event_date"]), str(row["event_id"])) > (
                str(prior["event_date"]), str(prior["event_id"])
            ):
                latest_until_replaced[key] = row
            continue
        half_life = int(row["half_life_td"] or 0)
        age = trading_days_between(str(row["event_date"]), as_of)
        if half_life > 0 and age <= half_life * 6:
            active.append(row)
    active.extend(latest_until_replaced.values())
    return active


def _provider_evidence_identity(row: dict[str, Any]) -> tuple[str, str] | None:
    event_type = str(row["event_type"])
    if event_type not in {
        "estimate_revision_up", "estimate_revision_down", "earnings_beat", "earnings_miss"
    }:
        return None
    parts = str(row.get("driver_tag", "")).split(":", 2)
    if len(parts) == 3:
        provider, metric, fiscal_period = parts
    else:
        # Legacy rows predate provider-qualified driver tags. Treat them as one
        # conservative evidence stream rather than allowing duplicate counting.
        provider, metric, fiscal_period = "legacy", str(row.get("driver_tag", "")), ""
    family = "estimate_revision" if event_type.startswith("estimate_revision_") else "earnings_surprise"
    period = fiscal_period or (str(row["event_date"]) if family == "earnings_surprise" else "unknown")
    return provider.casefold(), f"{family}:{metric.casefold()}:{period}"


def _effective_provider_events(
    rows: list[dict[str, Any]], *, as_of: str, points_per_unit: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Use provider observations once without averaging or directional bias."""
    passthrough: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        identity = _provider_evidence_identity(row)
        if identity is None:
            passthrough.append(row)
            continue
        provider, logical_key = identity
        current = grouped[logical_key].get(provider)
        if current is None or (str(row["event_date"]), str(row["event_id"])) > (
            str(current["event_date"]), str(current["event_id"])
        ):
            grouped[logical_key][provider] = row

    selected = list(passthrough)
    conflicts: list[dict[str, Any]] = []
    for logical_key, by_provider in sorted(grouped.items()):
        candidates = list(by_provider.values())
        directions = {1 if float(row["direction"]) > 0 else -1 for row in candidates}
        if len(directions) > 1:
            conflicts.append(
                {
                    "logical_key": logical_key,
                    "event_ids": sorted(str(row["event_id"]) for row in candidates),
                }
            )
            continue
        # Do not average agreeing vendors. Use one conservative observation and
        # retain every raw/provider event in the event store for reconciliation.
        representative = min(
            candidates,
            key=lambda row: (
                abs(decayed_event_points(row, as_of=as_of, points_per_unit=points_per_unit)),
                str(row["event_id"]),
            ),
        )
        selected.append(representative)
    return sorted(selected, key=lambda row: (str(row["event_date"]), str(row["event_id"]))), conflicts


def _severity_rank(state: str) -> int:
    return {"green": 0, "stable": 1, "watch": 2, "deteriorating": 3, "broken": 4}[state]


def _isolate_missing_market_action(
    action: str,
    *,
    market_data_status: str,
    is_holding: bool,
    is_target: bool,
    investable: bool,
) -> str:
    if market_data_status == "current":
        return action
    if market_data_status != "missing_latest":
        raise ValueError(f"Unknown market data status: {market_data_status}")
    if action in {"deteriorating", "exit_review"}:
        return action
    return "suspend_adds" if is_holding or is_target or investable else "watch"


def _positive_confirmation(events: list[dict[str, Any]]) -> bool:
    return any(
        row["event_type"] in {"guidance_affirmed", "guidance_raise", "earnings_beat", "estimate_revision_up", "customer_win_or_major_contract"}
        and float(row["direction"]) > 0
        for row in events
    )


def _apply_transition_policy(
    conn: Any,
    *,
    ticker: str,
    candidate: str,
    prior: str,
    les_total: float,
    events: list[dict[str, Any]],
    as_of: str,
) -> tuple[str, str, int]:
    if not prior or candidate == prior:
        return candidate, "score_cross" if not prior else "no_change", 0
    if _severity_rank(candidate) > _severity_rank(prior):
        return candidate, "score_cross", 0
    if prior == "broken":
        return prior, "manual_required", 0
    confirmation = _positive_confirmation(events)
    prior_rows = conn.execute(
        "SELECT run_as_of,les_total FROM les_snapshots "
        "WHERE ticker=? AND run_as_of<? ORDER BY run_as_of DESC LIMIT 10",
        (ticker, as_of),
    ).fetchall()
    def consecutive_dwell(threshold: float, maximum: int) -> int:
        count = 0
        previous_date = date.fromisoformat(as_of)
        for row in prior_rows[:maximum]:
            row_date = date.fromisoformat(str(row["run_as_of"]))
            if count and trading_days_between(row_date.isoformat(), previous_date.isoformat()) != 1:
                break
            if float(row["les_total"]) < threshold:
                break
            count += 1
            previous_date = row_date
        return count

    stable_dwell = consecutive_dwell(-5.0, 10)
    green_dwell = consecutive_dwell(20.0, 5)
    if prior == "deteriorating":
        if confirmation and les_total >= -20.0:
            return "watch", "upgrade_confirmation", min(5, stable_dwell)
        return prior, "upgrade_blocked", stable_dwell
    if prior == "watch":
        if confirmation or (les_total >= -5.0 and stable_dwell >= 9):
            return "stable", "upgrade_confirmation" if confirmation else "upgrade_dwell", stable_dwell + 1
        return prior, "upgrade_blocked", stable_dwell
    if prior == "stable" and candidate == "green":
        if confirmation or (les_total >= 20.0 and green_dwell >= 4):
            return "green", "upgrade_confirmation" if confirmation else "upgrade_dwell", green_dwell + 1
        return prior, "upgrade_blocked", green_dwell
    return candidate, "score_cross", stable_dwell


def _escalations(
    events: list[dict[str, Any]], market: dict[str, Any]
) -> tuple[list[str], str, list[str]]:
    flags: list[str] = []
    floor_state = "green"
    evidence: list[str] = []
    types = {str(row["event_type"]) for row in events}
    if "guidance_cut" in types:
        flags.append("R1")
        floor_state = "watch"
        evidence.extend(str(row["event_id"]) for row in events if row["event_type"] == "guidance_cut")
    r2 = [
        row for row in events
        if row["event_type"] in {"channel_check_negative", "churn_or_pricing_pressure_report"}
        and float(row["severity"]) >= 3.5
    ]
    if r2:
        flags.append("R2")
        floor_state = "deteriorating" if any(float(row["severity"]) >= 4.5 for row in r2) else max(
            floor_state, "watch", key=_severity_rank
        )
        evidence.extend(str(row["event_id"]) for row in r2)
    abnormal = finite_number(market.get("abnormal_ret_1d_z"))
    if abnormal is not None and abnormal <= -2.0 and any(float(row["direction"]) < 0 for row in events):
        flags.append("R4")
        floor_state = max(floor_state, "watch", key=_severity_rank)
    if "estimate_revision_down" in types:
        rel5 = finite_number(market.get("rel_ret_5d"))
        rel20 = finite_number(market.get("rel_ret_20d"))
        if rel5 is None or rel20 is None:
            flags.append("R5_UNAVAILABLE")
        elif rel5 < 0 and rel20 < 0:
            flags.append("R5")
            floor_state = max(floor_state, "watch", key=_severity_rank)
    if any(float(row["direction"]) < 0 and int(row["material_flag"]) for row in events):
        flags.append("R6")
    return sorted(set(flags)), floor_state, sorted(set(evidence))


def run_selftest() -> None:
    assert _event_family("guidance_cut") == "guidance"
    assert _severity_rank("broken") > _severity_rank("watch")
    flags, floor, _ = _escalations(
        [{"event_type": "guidance_cut", "severity": 4.5, "event_id": "x", "direction": -1, "material_flag": 1}],
        {"abnormal_ret_1d_z": -2.1, "rel_ret_5d": -0.1, "rel_ret_20d": -0.2},
    )
    assert "R1" in flags and floor == "watch"
    blank_flags, blank_floor, _ = _escalations(
        [{"event_type": "guidance_cut", "severity": 4.5, "event_id": "x", "direction": -1, "material_flag": 1}],
        {"abnormal_ret_1d_z": "", "rel_ret_5d": "", "rel_ret_20d": ""},
    )
    assert "R4" not in blank_flags and "R1" in blank_flags and blank_floor == "watch"
    assert _isolate_missing_market_action(
        "hold",
        market_data_status="missing_latest",
        is_holding=True,
        is_target=False,
        investable=False,
    ) == "suspend_adds"
    assert _isolate_missing_market_action(
        "watch",
        market_data_status="missing_latest",
        is_holding=False,
        is_target=False,
        investable=False,
    ) == "watch"
    common = {
        "event_date": "2026-07-01", "impact_0": 2.5, "decay_mode": "half_life",
        "half_life_td": 30, "category": "external_intel", "review_status": "auto",
    }
    agreeing, conflicts = _effective_provider_events(
        [
            {**common, "event_id": "fmp", "event_type": "estimate_revision_up", "direction": 1.0,
             "driver_tag": "fmp:eps:2026-12-31"},
            {**common, "event_id": "alpha", "event_type": "estimate_revision_up", "direction": 1.0,
             "driver_tag": "alpha_vantage:eps:2026-12-31"},
        ],
        as_of="2026-07-02", points_per_unit=4.0,
    )
    assert len(agreeing) == 1 and not conflicts
    conflicting, conflicts = _effective_provider_events(
        [
            {**common, "event_id": "fmp", "event_type": "estimate_revision_up", "direction": 1.0,
             "driver_tag": "fmp:eps:2026-12-31"},
            {**common, "event_id": "alpha", "event_type": "estimate_revision_down", "direction": -1.0,
             "impact_0": -2.5, "driver_tag": "alpha_vantage:eps:2026-12-31"},
        ],
        as_of="2026-07-02", points_per_unit=4.0,
    )
    assert not conflicting and len(conflicts) == 1
    assert _index_unique(
        [{"ticker": "AAA"}, {"ticker": "BBB"}], key="ticker", label="test"
    ) == {"AAA": {"ticker": "AAA"}, "BBB": {"ticker": "BBB"}}
    print("expectations state build selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    state_cfg = cfg_get(config, "expectations_monitor.state_model", {})
    if not isinstance(monitor_cfg, dict) or not isinstance(state_cfg, dict):
        raise ValueError("monitor/state config must be mappings")
    if state_cfg.get("policy_version") != "expectations_state_model_v1":
        raise ValueError("expectations_state_model_v1 config is required")
    if state_cfg.get("upgrades_require_dwell_or_confirmation") is not True:
        raise ValueError(
            "State upgrades must require dwell or positive confirmation"
        )
    if state_cfg.get("broken_requires_confirmed_thesis_break") is not True:
        raise ValueError("Broken state must require a confirmed thesis break")
    as_of = args.as_of.isoformat()
    universe_as_of = (args.universe_as_of or args.as_of).isoformat()
    monitor_subdir = monitor_output_subdir(config)
    events_dir = (
        args.events_dir
        or paths.output_dir / "runs" / as_of / monitor_subdir / "events"
    )
    signals_dir = (
        args.signals_dir
        or paths.output_dir / "runs" / as_of / monitor_subdir / "signals"
    )
    events_manifest_path = events_dir / "event_classification_manifest.json"
    signals_manifest_path = signals_dir / "market_signals_manifest.json"
    _check_manifest(
        events_manifest_path,
        as_of=as_of,
        accepted={"PASS", "PASS_WITH_DEFERRED"},
    )
    _check_manifest(signals_manifest_path, as_of=as_of, accepted={"PASS"})
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        universe = fetch_universe_snapshot(conn, universe_as_of)
        market_rows = _index_unique(
            _read_csv_rows(signals_dir / "market_signals.csv"),
            key="ticker",
            label="sealed market signals",
        )
        event_rows = _read_csv_rows(events_dir / "events.csv")
        all_events_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in event_rows:
            all_events_by_ticker[str(event["ticker"])].append(event)
        events_by_ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in _active_events(event_rows, as_of=as_of):
            events_by_ticker[str(event["ticker"])].append(event)
        percentiles = _percentiles(universe)
        now = utc_now()
        points_per_unit = float(state_cfg.get("points_per_unit", 4.0))
        baseline_scale = float(state_cfg.get("baseline_scale", 0.6))
        output_dir = (
            args.output_dir
            or paths.output_dir / "runs" / as_of / monitor_subdir
        )
        state_path = output_dir / "expectations_state.csv"
        transition_path = output_dir / "state_transitions.csv"
        manifest_path = output_dir / "expectations_state_manifest.json"
        fail_if_exists([state_path, transition_path, manifest_path], force=args.force)
        baseline_cap = float(state_cfg.get("baseline_cap", 30.0))
        peer_cap = float(state_cfg.get("peer_component_cap", 10.0))
        rows: list[dict[str, Any]] = []
        transitions: list[dict[str, Any]] = []
        for member in sorted(universe, key=lambda row: str(row["ticker"])):
            ticker = str(member["ticker"])
            confidence = float(member["score_confidence"] or 0.0)
            percentile = percentiles.get(ticker, 50.0)
            baseline = max(
                -baseline_cap,
                min(baseline_cap, (percentile - 50.0) * baseline_scale),
            ) * confidence
            raw_ticker_events = events_by_ticker.get(ticker, [])
            ticker_events, provider_conflicts = _effective_provider_events(
                raw_ticker_events, as_of=as_of, points_per_unit=points_per_unit
            )
            components = {"company": 0.0, "external": 0.0, "peer": 0.0}
            contributors: list[dict[str, Any]] = []
            for event in ticker_events:
                points = decayed_event_points(event, as_of=as_of, points_per_unit=points_per_unit)
                category = str(event["category"])
                key = "external" if category == "external_intel" else "peer" if category == "peer_readthrough" else "company"
                components[key] += points
                contributors.append({"event_id": event["event_id"], "event_type": event["event_type"], "points": round(points, 8)})
            market = market_rows.get(ticker)
            if market is None:
                raise ValueError(f"Missing same-date market signal for {ticker}")
            market_data_status = str(market.get("market_data_status", ""))
            if market_data_status not in {"current", "missing_latest"}:
                raise ValueError(
                    f"Invalid market data status for {ticker}: {market_data_status}"
                )
            market_points = float(market["market_component_points"])
            company = components["company"]
            external = components["external"]
            peer = max(-peer_cap, min(peer_cap, components["peer"]))
            les_total = max(-100.0, min(100.0, baseline + company + external + market_points + peer))
            guidance_cuts = [
                event
                for event in all_events_by_ticker.get(ticker, [])
                if event["event_type"] == "guidance_cut"
                and event["review_status"] != "dismissed"
                and trading_days_between(str(event["event_date"]), as_of) <= 130
            ]
            confirmed_break = any(
                int(event["thesis_break_flag"]) and event["review_status"] == "confirmed"
                for event in ticker_events
            ) or len({str(event["event_date"]) for event in guidance_cuts}) >= 2
            candidate = internal_state_for(les_total, confirmed_thesis_break=confirmed_break)
            flags, floor_state, evidence_ids = _escalations(ticker_events, market)
            if provider_conflicts:
                flags.append("PROVIDER_CONFLICT")
                evidence_ids.extend(
                    event_id
                    for conflict in provider_conflicts
                    for event_id in conflict["event_ids"]
                )
                contributors.extend(
                    {
                        "event_id": conflict["logical_key"],
                        "event_type": "provider_conflict",
                        "points": 0.0,
                    }
                    for conflict in provider_conflicts
                )
                flags = sorted(set(flags))
                evidence_ids = sorted(set(evidence_ids))
            if market_data_status == "missing_latest":
                flags.append("MARKET_DATA_UNAVAILABLE")
            flags = sorted(set(flags))
            evidence_ids = sorted(set(evidence_ids))
            if _severity_rank(floor_state) > _severity_rank(candidate):
                candidate = floor_state
            prior_row = conn.execute(
                "SELECT internal_state FROM les_snapshots WHERE ticker=? AND run_as_of<? ORDER BY run_as_of DESC LIMIT 1",
                (ticker, as_of),
            ).fetchone()
            prior = str(prior_row["internal_state"]) if prior_row is not None else ""
            internal, trigger, dwell = _apply_transition_policy(
                conn,
                ticker=ticker,
                candidate=candidate,
                prior=prior,
                les_total=les_total,
                events=ticker_events,
                as_of=as_of,
            )
            action = action_state_for(
                internal,
                is_holding=bool(member["is_holding"]),
                is_target=bool(member["is_target"]),
                investable=bool(member["investable_eligible"]),
            )
            action = _isolate_missing_market_action(
                action,
                market_data_status=market_data_status,
                is_holding=bool(member["is_holding"]),
                is_target=bool(member["is_target"]),
                investable=bool(member["investable_eligible"]),
            )
            if action not in ACTION_STATES or internal not in INTERNAL_STATES:
                raise AssertionError("State mapping escaped the closed contract")
            source_digest = digest(
                {
                    "member": member,
                    "events": [event["event_id"] for event in raw_ticker_events],
                    "provider_conflicts": provider_conflicts,
                    "market": market,
                    "events_manifest": sha256_file(events_manifest_path),
                    "signals_manifest": sha256_file(signals_manifest_path),
                }
            )
            row = {
                "ticker": ticker, "run_as_of": as_of, "asof_ts": now,
                "tier": member["tier"], "is_holding": member["is_holding"], "is_target": member["is_target"],
                "investable_eligible": member["investable_eligible"], "source_pipeline": member["source_pipeline"],
                "sector": member["sector"], "industry": member["industry"], "rating": member["rating"],
                "final_score": member["final_score"], "score_confidence": confidence,
                "within_pipeline_percentile": percentile, "baseline_points": baseline,
                "company_event_points": company, "external_intel_points": external,
                "market_points": market_points, "peer_readthrough_points": peer,
                "les_total": les_total, "internal_state": internal, "action_state": action,
                "market_data_status": market_data_status,
                "prior_internal_state": prior, "state_changed": int(bool(prior) and prior != internal),
                "escalation_flags_json": json.dumps(flags, separators=(",", ":")),
                "top_contributors_json": json.dumps(
                    sorted(contributors, key=lambda value: abs(float(value["points"])), reverse=True)[:3],
                    sort_keys=True, separators=(",", ":"),
                ),
                "input_digest": source_digest,
            }
            rows.append(row)
            if prior and prior != internal:
                transitions.append(
                    {
                        "transition_id": digest({"ticker": ticker, "run_as_of": as_of}),
                        "ticker": ticker, "transition_ts": now, "run_as_of": as_of,
                        "from_state": prior, "to_state": internal, "trigger": trigger,
                        "rule_id": ",".join(flags), "evidence_event_ids": json.dumps(evidence_ids, separators=(",", ":")),
                        "dwell_days_met": dwell, "approved_by": "auto", "note": "",
                    }
                )
        with database_writer_lock(db_path, timeout_sec=timeout), conn:
            conn.execute("DELETE FROM les_snapshots WHERE run_as_of=?", (as_of,))
            conn.execute("DELETE FROM state_transitions WHERE run_as_of=?", (as_of,))
            db_fields = [
                "ticker", "asof_ts", "run_as_of", "baseline_points", "company_event_points",
                "external_intel_points", "market_points", "peer_readthrough_points", "les_total",
                "internal_state", "action_state", "prior_internal_state", "state_changed",
                "market_data_status",
                "escalation_flags_json", "top_contributors_json", "input_digest",
            ]
            conn.executemany(
                f"INSERT INTO les_snapshots({','.join(db_fields)}) VALUES ({','.join('?' for _ in db_fields)})",
                [tuple(row[field] for field in db_fields) for row in rows],
            )
            if transitions:
                conn.executemany(
                    f"INSERT INTO state_transitions({','.join(TRANSITION_FIELDS)}) VALUES ({','.join('?' for _ in TRANSITION_FIELDS)})",
                    [tuple(row[field] for field in TRANSITION_FIELDS) for row in transitions],
                )
    finally:
        conn.close()
    write_csv(state_path, STATE_FIELDS, rows)
    write_csv(transition_path, TRANSITION_FIELDS, transitions)
    input_paths = [config_path, Path(__file__).resolve(), Path(__file__).with_name("state_common.py"), events_manifest_path, signals_manifest_path]
    write_manifest(
        manifest_path,
        {
            "schema_version": "expectations_state_manifest_v1",
            "acceptance": "PASS",
            "as_of_date": as_of,
            "universe_as_of": universe_as_of,
            "row_count": len(rows),
            "transition_count": len(transitions),
            "shadow_only": True,
            "broker_execution_prohibited": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {state_path.name: sha256_file(state_path), transition_path.name: sha256_file(transition_path)},
        },
    )
    print("EXPECTATIONS STATE: PASS")
    print(f"rows={len(rows)}; transitions={len(transitions)}; manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
