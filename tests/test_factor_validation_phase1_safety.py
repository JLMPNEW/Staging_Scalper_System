from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_module(relative_path: str, name: str) -> Any:
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = list(rows[0])
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_med_device_ic_tilt_is_shadow_only() -> None:
    config_module = load_module("med_devices/core/config.py", "phase1_med_config")
    score_module = load_module(
        "med_devices/scripts/13_build_med_device_daily_scores.py",
        "phase1_med_scores",
    )
    config_path = ROOT / "med_devices/config.yaml"
    config = config_module.load_yaml(config_path)

    policy = score_module.load_ic_tilted_composite_policy(
        config,
        base_dir=config_path.parent,
    )

    assert policy["enabled"] is True
    assert policy["mode"] == "shadow"
    assert policy["allow_replace"] is False


def test_biotech_pooled_monitor_marks_every_result_research_only() -> None:
    module = load_module(
        "biotech_index/scripts/43_validate_biotech_feature_ic_monotonicity.py",
        "phase1_biotech_monitor",
    )
    values = [
        (
            {"ticker": f"T{index}", "asof_date": f"2026-01-{index + 1:02d}"},
            float(index),
            float(index) / 10.0,
        )
        for index in range(10)
    ]

    summary, _quintiles = module.summary_for_group(
        factor="test_factor",
        horizon=120,
        cohort="ALL",
        source_group="ALL",
        values=values,
        lcb_z=1.0,
        min_observations=1,
        min_quintile_observations=1,
    )

    assert summary["factor_validation_contract"] == "biotech_feature_ic_pooled_v0"
    assert summary["evidence_status"] == "legacy_pooled_research_only"
    assert summary["promotion_eligible"] == 0
    assert "overlap_adjusted" in summary["promotion_blocker"]


def test_biotech_optimizer_runs_legacy_candidates_as_research_only(tmp_path: Path) -> None:
    module = load_module(
        "biotech_index/scripts/46_optuna_biotech_candidate_optimizer.py",
        "phase1_biotech_optimizer_legacy",
    )
    write_csv(
        tmp_path / "feature_ic_classification.csv",
        [
            {
                "factor": "legacy_factor",
                "classification": "promote_candidate",
                "factor_validation_contract": "biotech_feature_ic_pooled_v0",
                "evidence_status": "legacy_pooled_research_only",
                "promotion_eligible": "0",
            }
        ],
    )

    gates, counts, authorization = module.validate_ic(tmp_path, min_promotable=1)

    assert counts == {"promote_candidate": 1}
    assert authorization["authorization_status"] == "research_only"
    assert authorization["production_promotion_authorized"] is False
    assert authorization["research_candidate_factor_count"] == 1
    assert authorization["authorized_factor_count"] == 0
    authorization_gate = next(
        row for row in gates if row["gate"] == "feature_ic_production_promotion_authorization"
    )
    assert authorization_gate["status"] == "WARN"
    assert not [row for row in gates if row["status"] == "FAIL"]


def test_biotech_optimizer_only_authorizes_shared_contract(tmp_path: Path) -> None:
    module = load_module(
        "biotech_index/scripts/46_optuna_biotech_candidate_optimizer.py",
        "phase1_biotech_optimizer_authorized",
    )
    write_csv(
        tmp_path / "feature_ic_classification.csv",
        [
            {
                "factor": "shared_factor",
                "classification": "promote_candidate",
                "factor_validation_contract": "factor_validation_v1",
                "evidence_status": "authoritative",
                "promotion_eligible": "1",
            }
        ],
    )

    gates, _counts, authorization = module.validate_ic(tmp_path, min_promotable=1)

    assert authorization["authorization_status"] == "authorized"
    assert authorization["production_promotion_authorized"] is True
    assert authorization["authorized_factor_count"] == 1
    authorization_gate = next(
        row for row in gates if row["gate"] == "feature_ic_production_promotion_authorization"
    )
    assert authorization_gate["status"] == "PASS"
