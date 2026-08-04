#!/usr/bin/env python3
"""Run the pre-registered transportation v3 research preflight (read-only)."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.transportation.selected_feature_history import (  # noqa: E402
    read_only_connection,
    sha256,
    write_manifest,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)
from industrials.transportation.v3_preflight import (  # noqa: E402
    BREADTH_FIELDS,
    COVERAGE_FIELDS,
    PREFLIGHT_VERSION,
    STABILITY_FIELDS,
    build_signal_values,
    forward_excess_returns,
    iter_surface_generic_rows,
    load_memberships,
    load_prices,
    read_peer_groups,
    resolve_post_merge,
    stability_rows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only transportation v3 preflight: full-PIT peer-group "
            "breadth, coverage, and signal-stability diagnostics under the "
            "pre-registered contract. Produces design evidence only."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Defaults to output/industrials/transportation/v3_preflight.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    policy_path = (
        base_dir / "transportation" / "data"
        / "transportation_v3_preflight_policy.yaml"
    )
    policy = load_yaml(policy_path)
    peer_path = resolve_path(
        str(policy["universe"]["peer_group_map"]), base_dir=base_dir
    )
    panel_path = resolve_path(
        "".join(str(policy["panel"]["complete_panel"]).split()),
        base_dir=base_dir,
    )
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else PROJECT_ROOT / "output" / "industrials" / MODEL_FAMILY / "v3_preflight"
    )
    for path in (policy_path, peer_path, panel_path, db_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    peer_groups = read_peer_groups(peer_path)
    expected = int(policy["universe"]["expected_member_count"])
    if len(peer_groups) != expected:
        raise ValueError(
            f"peer-group map rows={len(peer_groups)} expected={expected}"
        )
    excluded = frozenset(
        str(value) for value in policy["panel"]["excluded_metrics"]
    )
    signals = {
        str(signal_id): (
            str(spec[0]),
            str(spec[1]),
            int(spec[2]),
        )
        for signal_id, spec in policy["candidate_signals"].items()
    }
    gates = policy["gates"]
    horizons = [int(value) for value in policy["outcomes"]["horizons_sessions"]]
    strides = {
        str(key): int(value)
        for key, value in policy["outcomes"]["non_overlap_stride"].items()
    }
    regime_split = str(policy["outcomes"]["regime_split_date"])
    benchmark = str(policy["outcomes"]["benchmark"])

    panel_rows = list(
        iter_surface_generic_rows(panel_path, excluded_metrics=excluded)
    )
    dates = sorted({row["asof_date"] for row in panel_rows})
    panel_tickers = {row["ticker"] for row in panel_rows}
    unmapped = sorted(panel_tickers - set(peer_groups))
    if unmapped:
        raise ValueError(f"panel surface tickers missing peer group={unmapped}")

    historical = family["historical_load"]
    active_source = str(historical["active_price_source_id"])
    delisted_source = str(historical["delisted_price_source_id"])
    with read_only_connection(db_path) as connection:
        memberships = load_memberships(
            connection,
            source_id=str(universe["historical_membership_source_id"]),
        )
        prices = load_prices(
            connection,
            tickers=[*peer_groups, benchmark],
            sources=(active_source, delisted_source),
        )
    missing_membership = sorted(
        ticker for ticker in peer_groups if ticker not in memberships
    )

    breadth_cfg = gates["breadth"]
    post_merge, breadth_report = resolve_post_merge(
        peer_groups,
        memberships=memberships,
        dates=dates,
        minimum_mean=float(breadth_cfg["minimum_mean_members"]),
        minimum_floor=float(breadth_cfg["minimum_floor_members"]),
    )

    signal_values = build_signal_values(
        panel_rows, signals=signals, dates=dates
    )
    coverage_counter: dict[tuple[str, str], list[int]] = defaultdict(
        lambda: [0, 0]
    )
    for row in panel_rows:
        group = post_merge[peer_groups[row["ticker"]].peer_group]
        bucket = coverage_counter[(group, row["metric_id"])]
        bucket[0] += 1
        if row["metric_value"] != "":
            bucket[1] += 1
    coverage_report = [
        {
            "post_merge_group": group,
            "metric_id": metric_id,
            "member_date_rows": totals[0],
            "observed_rows": totals[1],
            "coverage_rate": round(totals[1] / totals[0], 6) if totals[0] else 0.0,
        }
        for (group, metric_id), totals in sorted(coverage_counter.items())
    ]

    stability_cfg = gates["signal_stability"]
    stability_report: list[dict[str, Any]] = []
    for horizon in horizons:
        excess = forward_excess_returns(
            prices=prices,
            memberships=memberships,
            peer_groups=peer_groups,
            benchmark=benchmark,
            dates=dates,
            horizon=horizon,
            active_source=active_source,
            delisted_source=delisted_source,
        )
        stability_report.extend(
            stability_rows(
                signals=signals,
                signal_values=signal_values,
                excess=excess,
                memberships=memberships,
                peer_groups=peer_groups,
                post_merge=post_merge,
                dates=dates,
                horizon=horizon,
                stride=strides[str(horizon)],
                regime_split=regime_split,
                minimum_total_periods=int(
                    stability_cfg["minimum_total_periods"]
                ),
                minimum_regime_periods=int(
                    stability_cfg["minimum_periods_per_regime"]
                ),
                minimum_abs_ic=float(stability_cfg["minimum_abs_mean_ic"]),
            )
        )

    decision_cfg = gates["decision"]["build_submodels_if"]
    qualifying: dict[str, int] = defaultdict(int)
    for row in stability_report:
        if row["horizon_sessions"] == 63 and row["qualifies"]:
            qualifying[str(row["post_merge_group"])] += 1
    minimum_signals = int(
        gates["group_model_eligible"]["minimum_qualifying_signals"]
    )
    eligible_groups = sorted(
        group
        for group, count in qualifying.items()
        if count >= minimum_signals
    )
    members_by_post_merge: dict[str, int] = defaultdict(int)
    for row in peer_groups.values():
        members_by_post_merge[post_merge[row.peer_group]] += 1
    covered = sum(members_by_post_merge[group] for group in eligible_groups)
    share = covered / len(peer_groups) if peer_groups else 0.0
    build = (
        len(eligible_groups)
        >= int(decision_cfg["minimum_model_eligible_groups"])
        and share >= float(decision_cfg["minimum_covered_membership_share"])
    )
    decision = "BUILD_V3_SUBMODELS" if build else "SLEEVE_ONLY"

    output_dir.mkdir(parents=True, exist_ok=True)
    breadth_path = output_dir / "transportation_v3_preflight_breadth.csv"
    coverage_path = output_dir / "transportation_v3_preflight_coverage.csv"
    stability_path = output_dir / "transportation_v3_preflight_stability.csv"
    manifest_path = output_dir / "transportation_v3_preflight_manifest.json"
    write_csv_atomic(breadth_path, BREADTH_FIELDS, breadth_report)
    write_csv_atomic(coverage_path, COVERAGE_FIELDS, coverage_report)
    write_csv_atomic(stability_path, STABILITY_FIELDS, stability_report)
    payload: dict[str, Any] = {
        "acceptance": "PASS",
        "gate": "V3_PREFLIGHT_DESIGN_DIAGNOSTICS",
        "preflight_version": PREFLIGHT_VERSION,
        "model_family": MODEL_FAMILY,
        "evidence_status": "revealed_research_design_only",
        "production_promotion_authorized": False,
        "panel_date_count": len(dates),
        "surface_panel_ticker_count": len(panel_tickers),
        "peer_group_member_count": len(peer_groups),
        "members_without_membership_rows": missing_membership,
        "post_merge_groups": sorted(set(post_merge.values())),
        "post_merge_map": dict(sorted(post_merge.items())),
        "qualifying_signal_counts_63s": dict(sorted(qualifying.items())),
        "model_eligible_groups": eligible_groups,
        "covered_membership_share": round(share, 6),
        "architecture_decision": decision,
        "null_model": policy["null_model"],
        "inputs": {
            "policy": {"path": str(policy_path), "sha256": sha256(policy_path)},
            "peer_groups": {"path": str(peer_path), "sha256": sha256(peer_path)},
            "complete_panel": {
                "path": str(panel_path),
                "sha256": sha256(panel_path),
            },
            "database_path": str(db_path),
        },
        "artifacts": {
            "breadth": {"path": str(breadth_path), "sha256": sha256(breadth_path)},
            "coverage": {
                "path": str(coverage_path),
                "sha256": sha256(coverage_path),
            },
            "stability": {
                "path": str(stability_path),
                "sha256": sha256(stability_path),
            },
        },
        "operations": {
            "database_mode": "read_only",
            "database_writes": 0,
            "network_requests": 0,
            "parser_invocations": 0,
            "feature_rebuilds": 0,
            "calibration_invocations": 0,
            "portfolio_writes": 0,
        },
        "next_gate": (
            "AUTHOR_V3_SUBMODEL_CONTRACT"
            if decision == "BUILD_V3_SUBMODELS"
            else "ADOPT_ELIGIBILITY_SLEEVE_ONLY_ARCHITECTURE"
        ),
    }
    write_manifest(manifest_path, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
