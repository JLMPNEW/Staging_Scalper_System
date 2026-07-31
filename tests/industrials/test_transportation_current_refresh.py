from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    PROJECT_ROOT
    / "industrials"
    / "transportation"
    / "scripts"
    / "16_run_transportation_current_refresh.py"
)


def _module():
    spec = importlib.util.spec_from_file_location(
        "transportation_current_refresh_script",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_current_refresh_step_graph_is_bounded_and_complete() -> None:
    module = _module()
    steps = module.build_steps("2026-07-30")
    identifiers = [step.step_id for step in steps]
    assert len(identifiers) == len(set(identifiers))
    assert identifiers[0] == "00_validate_seed"
    assert identifiers[-1] == "21a_audit_monitor"
    assert "03_sync_prices" in identifiers
    assert "07_sync_sec" in identifiers
    assert "11_sync_fx" in identifiers
    disclosure_step = next(
        step for step in steps if step.step_id == "08c_sync_disclosures"
    )
    assert "--active-only" not in disclosure_step.args
    scoring_step = next(step for step in steps if step.step_id == "06a_build_scoring")
    assert "--force" in scoring_step.args
    pit_step = next(step for step in steps if step.step_id == "19_build_exact_pit")
    assert "--output-csv" in pit_step.args
    assert "--output-json" in pit_step.args
    assert any("current_panels/2026-07-30" in value for value in pit_step.args)
    assert "13_sync_positioning" in identifiers
    assert "19_build_exact_pit" in identifiers
    assert "19j_build_current_panel" in identifiers
    assert "21b_export_monitor_source" in identifiers
    assert not any("19c_materialize" in step.script for step in steps)
    assert not any("19h_run" in step.script for step in steps)
    assert not any("walk_forward_outcome" in step.script for step in steps)


def test_current_refresh_can_reuse_already_refreshed_positioning_raw() -> None:
    module = _module()
    steps = module.build_steps(
        "2026-07-30",
        skip_positioning_upstream=True,
    )
    identifiers = [step.step_id for step in steps]
    assert "13_sync_positioning" not in identifiers
    assert "09_import_positioning" in identifiers
    assert "14_validate_positioning" in identifiers


def test_step_selection_is_ordered_and_rejects_reverse_range() -> None:
    module = _module()
    steps = module.build_steps("2026-07-30")
    selected = module.select_steps(
        steps,
        from_step="19_build_exact_pit",
        to_step="08a_validate_metrics",
    )
    assert [step.step_id for step in selected] == [
        "19_build_exact_pit",
        "06_validate_market",
        "08_validate_financial",
        "08a_validate_metrics",
    ]
    with pytest.raises(ValueError, match="must not follow"):
        module.select_steps(
            steps,
            from_step="17_publish_shadow",
            to_step="03_sync_prices",
        )
