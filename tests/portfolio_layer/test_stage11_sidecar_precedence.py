from __future__ import annotations

# pyright: reportAttributeAccessIssue=false

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "portfolio_layer" / "research" / "67_join_calibration_panel.py"


def load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("stage11_join_precedence_test", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_sidecar(path: Path, status: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=(
                "asof_date",
                "ticker",
                "survivorship_corrected_panel_flag",
                "stage11_calibration_input_eligible_flag",
                "calibration_sample_role",
                "membership_status",
                "terminal_date",
                "score_recomputed_pit_flag",
            ),
        )
        writer.writeheader()
        writer.writerow(
            {
                "asof_date": "2022-05-02",
                "ticker": "TEST",
                "survivorship_corrected_panel_flag": "1",
                "stage11_calibration_input_eligible_flag": "1",
                "calibration_sample_role": "pre_lock_research",
                "membership_status": status,
                "terminal_date": "",
                "score_recomputed_pit_flag": "1",
            }
        )


def test_certified_range_chunk_supersedes_stale_per_date_sidecar(tmp_path: Path) -> None:
    module = load_module()
    dashboard = tmp_path / "dashboard"
    rank_path = dashboard / "2022-05-02" / "software_infrastructure_final_rank_table.csv"
    rank_path.parent.mkdir(parents=True)
    rank_path.write_text("ticker,asof_date\nTEST,2022-05-02\n", encoding="utf-8")
    write_sidecar(
        dashboard / "software_infrastructure_stage11_survivorship_calibration_panel.csv",
        "root_fallback",
    )
    write_sidecar(
        rank_path.with_name("software_infrastructure_stage11_survivorship_calibration_panel.csv"),
        "stale_per_date",
    )
    write_sidecar(
        dashboard
        / "stage11_combined"
        / "software_infrastructure_stage11_survivorship_calibration_panel_2019-01-04_2026-08-25.csv",
        "certified_chunk",
    )

    module.dated_candidates = lambda _cfg, _root: [("20220502", rank_path)]
    config = {
        "score_contract": {
            "sector_output_root": str(tmp_path),
            "sectors": [
                {
                    "enabled": True,
                    "file_mode": "dated",
                    "file_path": "software_infrastructure_final_rank_table.csv",
                    "model_family": "software_infrastructure",
                }
            ],
        }
    }
    used: dict[str, str] = {}
    index = module.build_sidecar_index(config, tmp_path / "config.yaml", used)

    row = index["software_infrastructure"]["2022-05-02"]["TEST"]
    assert row["sidecar_membership_status"] == "certified_chunk"
    assert len(used) == 3
