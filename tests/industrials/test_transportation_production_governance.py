from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

from industrials.core.oos_research import artifact_sha256
from industrials.core.production_lock import (
    PRODUCTION_LOCK_FIELDS,
    append_production_lock,
    load_effective_production_lock,
)
from industrials.core.reports import write_csv_atomic
from industrials.transportation.contracts import COMPONENT_FIELDS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "industrials" / "transportation" / "scripts"


def load_script(name: str):
    path = SCRIPTS / name
    spec = importlib.util.spec_from_file_location(
        "transportation_governance_" + name.replace(".", "_"),
        path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_production_clis_separate_research_and_effective_dates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    promoter = load_script("27_promote_transportation_oos_production.py")
    monkeypatch.setattr(sys, "argv", ["promote.py"])
    promotion_args = promoter.parse_args()
    assert promotion_args.research_asof == "2026-07-30"
    assert promotion_args.effective_date == "2026-07-31"

    activation = load_script("31_activate_transportation_oos_production.py")
    monkeypatch.setattr(sys, "argv", ["activate.py"])
    activation_args = activation.parse_args()
    assert activation_args.research_asof == "2026-07-30"
    assert activation_args.effective_date == "2026-07-31"


def test_transportation_shared_lock_resolves_and_detects_tampering(
    tmp_path: Path,
) -> None:
    weights = {
        field: 1.0 / len(COMPONENT_FIELDS)
        for field in COMPONENT_FIELDS
    }
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(
        json.dumps(
            {
                "model_family": "transportation",
                "status": "pass",
                "promoted": True,
                "asof_date": "2026-07-31",
                "promotion_payload": {"weights": weights},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "locks.csv"
    write_csv_atomic(registry_path, PRODUCTION_LOCK_FIELDS, [])
    append_production_lock(
        registry_path=registry_path,
        row={
            "lock_id": "transportation_generic_oos_v1_20260731",
            "effective_from": "2026-07-31",
            "effective_to": "",
            "lock_date": "2026-07-30",
            "train_start_date": "2019-01-02",
            "train_end_date": "2024-01-31",
            "scoring_mode": "generic_oos",
            "score_model_version": "transportation_generic_oos_v1",
            "validation_method": "weekly_pit_panel_validation_ic_holdout_backtest",
            "decision_manifest_path": str(decision_path),
            "decision_manifest_sha256": artifact_sha256(decision_path),
            "enabled": "1",
            "created_at_utc": "2026-07-31T12:00:00+00:00",
        },
    )
    config = {
        "oos_calibration_standards": {
            "families": {
                "transportation": {
                    "production_lock_registry_csv": str(registry_path)
                }
            }
        }
    }
    lock = load_effective_production_lock(
        config,
        model_family="transportation",
        base_dir=tmp_path,
        asof="2026-07-31",
    )
    assert lock is not None
    assert lock.lock_id == "transportation_generic_oos_v1_20260731"
    assert lock.lock_date == date(2026, 7, 30)
    assert lock.effective_from == date(2026, 7, 31)
    assert lock.weights == pytest.approx(weights)

    decision_path.write_text(
        decision_path.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        load_effective_production_lock(
            config,
            model_family="transportation",
            base_dir=tmp_path,
            asof="2026-07-31",
        )
