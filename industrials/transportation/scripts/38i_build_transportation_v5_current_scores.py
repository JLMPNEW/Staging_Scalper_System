#!/usr/bin/env python3
"""Build a shadow-only current score snapshot for the isolated v5 cohorts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.core.db import connect, init_db  # noqa: E402
from industrials.core.reports import write_text_atomic  # noqa: E402
from industrials.transportation.contracts import (  # noqa: E402
    file_sha256,
    write_scoring_rows,
)
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.investable_universe import load_investable_universe_policy  # noqa: E402
from industrials.transportation.scoring import build_scoring_rows  # noqa: E402
from industrials.transportation.surface_freight_score_engine import load_cohort_score_policy  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


DEFAULT_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_investable_universe_v5.yaml"
DEFAULT_SURFACE_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_surface_freight_score_policy_v3.yaml"
DEFAULT_TANKER_POLICY = PROJECT_ROOT / "industrials" / "transportation" / "data" / "transportation_tanker_score_policy_v1.yaml"
DEFAULT_READINESS = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "current_readiness" / "2026-08-13" / "transportation_v5_current_readiness.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "output" / "industrials" / "transportation" / "investable_v5" / "current_scores"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--surface-policy", type=Path, default=DEFAULT_SURFACE_POLICY)
    parser.add_argument("--tanker-policy", type=Path, default=DEFAULT_TANKER_POLICY)
    parser.add_argument("--readiness", type=Path, default=DEFAULT_READINESS)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    asof = str(args.asof)[:10]
    config_path = args.config.expanduser().resolve()
    policy_path = args.policy.expanduser().resolve()
    surface_path = args.surface_policy.expanduser().resolve()
    tanker_path = args.tanker_policy.expanduser().resolve()
    readiness_path = args.readiness.expanduser().resolve()
    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    if readiness.get("acceptance") != "PASS" or readiness.get("asof_date") != asof:
        raise ValueError("v5 current-readiness gate is not a matching PASS")
    policy = load_investable_universe_policy(policy_path)
    expected = set(policy.selected_tickers)
    if (
        readiness.get("policy_version") != policy.policy_version
        or (readiness.get("artifacts") or {}).get("policy", {}).get("sha256")
        != file_sha256(policy_path)
    ):
        raise ValueError("readiness gate does not pin the current v5 policy")
    surface_policy = load_cohort_score_policy(surface_path)
    tanker_policy = load_cohort_score_policy(tanker_path)
    if set(surface_policy["eligible_tickers"]) | set(tanker_policy["eligible_tickers"]) != expected:
        raise ValueError("cohort score policies do not cover the exact v5 universe")
    if set(surface_policy["eligible_tickers"]) & set(tanker_policy["eligible_tickers"]):
        raise ValueError("cohort score policies overlap")

    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(
        cfg_get(config, "paths.database_path"), base_dir=base_dir
    )
    registry_path = resolve_path(
        family["financial"]["metric_registry"], base_dir=base_dir
    )
    registry_version, definitions = load_metric_registry(registry_path)
    scoring = family["scoring"]
    universe = family["universe"]
    component_weights = {
        str(key): float(value)
        for key, value in scoring["component_weights"].items()
    }
    overlay_weights = {
        str(key): float(value)
        for key, value in (scoring.get("specialized_overlay_weights") or {}).items()
    }
    if any(value != 0.0 for value in overlay_weights.values()):
        raise ValueError("v5 preflight requires legacy bounded overlays to remain zero")
    eligibility_path = resolve_path(
        cfg_get(config, "scoring_policy.families.transportation.eligibility_policy_csv"),
        base_dir=base_dir,
    )
    with connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120.0)),
    ) as connection:
        init_db(connection)
        rows = build_scoring_rows(
            connection,
            asof=asof,
            active_source_id=str(universe["seed_source_id"]),
            definitions=definitions,
            registry_version=registry_version,
            policy_path=eligibility_path,
            policy_asof=asof,
            component_weights=component_weights,
            max_staleness_days=int(scoring["max_staleness_days"]),
            minimum_avg_dollar_volume=float(scoring["minimum_avg_dollar_volume_60d"]),
            minimum_score_confidence=float(scoring["minimum_score_confidence"]),
            minimum_specialized_coverage=float(scoring["minimum_specialized_coverage"]),
            positioning_source_id=str(scoring["positioning_feature_source_id"]),
            minimum_positioning_input_coverage=float(scoring["minimum_positioning_input_coverage"]),
            specialized_overlay_weights=overlay_weights,
            classification_overlays_path=resolve_path(
                universe["classification_overlays_csv"], base_dir=base_dir
            ),
            cohort_score_policies=[surface_policy, tanker_policy],
        )
    actual = {row["ticker"] for row in rows}
    if actual != expected or len(rows) != len(expected):
        raise ValueError(
            f"v5 score output mismatch missing={sorted(expected-actual)} extra={sorted(actual-expected)}"
        )
    output_dir = args.output_root.expanduser().resolve() / asof
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "transportation_v5_scoring_features.csv"
    write_scoring_rows(output_path, rows)
    by_group = {
        group.group_id: {
            "row_count": sum(row["ticker"] in set(group.tickers) for row in rows),
            "rank_ready_count": sum(
                row["ticker"] in set(group.tickers) and row["rank_ready_flag"] == "1"
                for row in rows
            ),
            "blocked_tickers": sorted(
                row["ticker"]
                for row in rows
                if row["ticker"] in set(group.tickers)
                and row["rank_ready_flag"] != "1"
            ),
        }
        for group in policy.groups
    }
    result = {
        "acceptance": "PASS",
        "asof_date": asof,
        "contract_version": "transportation_v5_current_scores_v1",
        "row_count": len(rows),
        "rank_ready_count": sum(row["rank_ready_flag"] == "1" for row in rows),
        "blocked_count": sum(row["rank_ready_flag"] != "1" for row in rows),
        "cohort_results": by_group,
        "metric_registry_version": registry_version,
        "artifacts": {
            "readiness": {"path": str(readiness_path), "sha256": file_sha256(readiness_path)},
            "investable_policy": {"path": str(policy_path), "sha256": file_sha256(policy_path)},
            "surface_score_policy": {"path": str(surface_path), "sha256": file_sha256(surface_path)},
            "tanker_score_policy": {"path": str(tanker_path), "sha256": file_sha256(tanker_path)},
            "metric_registry": {"path": str(registry_path), "sha256": file_sha256(registry_path)},
            "scoring_features": {"path": str(output_path), "sha256": file_sha256(output_path)},
        },
        "network_requests": 0,
        "parser_invocations": 0,
        "historical_reconstruction_performed": False,
        "calibration_performed": False,
        "production_activation_performed": False,
        "next_gate": "FREEZE_V5_PREBUILD_CONTRACT" if all(
            row["rank_ready_flag"] == "1" for row in rows
        ) else "REVIEW_CURRENT_RANK_BLOCKS",
    }
    manifest_path = output_dir / "transportation_v5_current_scores.json"
    write_text_atomic(manifest_path, json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
