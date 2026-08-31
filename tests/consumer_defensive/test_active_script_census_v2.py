from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "consumer_defensive/scripts"
LEGACY_MANIFEST = ROOT / "archive/consumer_defensive_legacy_future_evidence_20260826/MANIFEST.json"


def test_active_script_census_keeps_v2_entrypoints_and_excludes_retired_routes() -> None:
    active = {path.name for path in SCRIPTS.glob("*.py")}
    required = {
        "00_init_consumer_defensive_db.py",
            "01_load_consumer_defensive_universe.py",
            "02_validate_consumer_defensive_universe.py",
            "02b_ensure_consumer_defensive_stage2.py",
        "03_sync_consumer_defensive_adjusted_prices.py",
        "07_sync_consumer_defensive_sec_fundamentals.py",
        "08_build_consumer_defensive_financial_features.py",
        "10_import_consumer_defensive_positioning.py",
        "12_build_consumer_defensive_scoring_features.py",
        "13a_run_consumer_defensive_specialized_parser.py",
        "13b_build_consumer_defensive_specialized_metrics.py",
        "14_build_consumer_defensive_stage6c_panel.py",
        "15_run_consumer_defensive_factor_validation.py",
        "26_validate_consumer_defensive_promotion_framework_v2.py",
        "27_run_consumer_defensive_v2_foundation.py",
        "28_preregister_consumer_defensive_calibration_v2.py",
        "29_run_consumer_defensive_calibration_v2.py",
        "29a_build_consumer_defensive_promotion_input_v3.py",
        "30_run_consumer_defensive_promotion_engine_v3.py",
        "30a_manage_consumer_defensive_promotion_evidence_v3.py",
        "31_publish_consumer_defensive_production_scores_v3.py",
        "32_run_consumer_defensive_production_refresh_v3.py",
    }
    # This is an intentional active-surface seal. Bump it only after reviewing
    # every added script against the retired-route manifest below.
    assert len(active) == 79
    assert required <= active
    manifest = json.loads(LEGACY_MANIFEST.read_text(encoding="utf-8"))
    retired_script_names = {
        Path(row["original_path"]).name
        for row in manifest["files"]
        if str(row["original_path"]).startswith("consumer_defensive/scripts/")
    }
    assert retired_script_names
    assert active.isdisjoint(retired_script_names)
    assert not {name for name in active if "audit_final" in name or name.startswith("_")}

