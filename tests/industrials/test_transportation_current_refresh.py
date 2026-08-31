from __future__ import annotations

import importlib.util
from pathlib import Path
import json
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
    assert "03a_sync_shares" in identifiers
    assert identifiers.index("07_sync_sec") < identifiers.index("03a_sync_shares")
    assert identifiers.index("03a_sync_shares") < identifiers.index("19_build_exact_pit")
    share_step = next(step for step in steps if step.step_id == "03a_sync_shares")
    assert share_step.network is True
    assert "--include-historical" not in share_step.args
    assert "--allow-partial" in share_step.args
    disclosure_step = next(
        step for step in steps if step.step_id == "08c_sync_disclosures"
    )
    assert "--active-only" not in disclosure_step.args
    scoring_step = next(step for step in steps if step.step_id == "06a_build_scoring")
    assert "--force" in scoring_step.args
    publisher_step = next(step for step in steps if step.step_id == "17_publish_shadow")
    assert "--force" not in publisher_step.args
    pit_step = next(step for step in steps if step.step_id == "19_build_exact_pit")
    assert "--rebuild-existing" in pit_step.args
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


def test_current_refresh_force_publish_is_explicit_and_scoped() -> None:
    module = _module()
    steps = module.build_steps("2026-07-30", force_publish=True)
    publisher_step = next(
        step for step in steps if step.step_id == "17_publish_shadow"
    )
    assert publisher_step.args == ["--asof", "2026-07-30", "--force"]
    assert all(
        "--force" not in step.args
        for step in steps
        if step.step_id not in {"06a_build_scoring", "17_publish_shadow"}
    )


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


def test_resume_requires_exact_compatible_failed_manifest(tmp_path: Path) -> None:
    module = _module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "acceptance": "FAIL",
                "asof_date": "2026-08-24",
                "orchestrator_version": module.ORCHESTRATOR_VERSION,
                "config_sha256": "config",
                "orchestrator_source_sha256": "source",
                "failed_step_ids": ["08c_validate_disclosures"],
            }
        ),
        encoding="utf-8",
    )
    valid_steps = [
        step.step_id for step in module.build_steps("2026-08-24")
    ]
    assert module.resume_step_from_manifest(
        manifest,
        asof="2026-08-24",
        valid_step_ids=valid_steps,
        config_sha256="config",
        orchestrator_source_sha256="source",
    ) == "08c_validate_disclosures"
    with pytest.raises(ValueError, match="stale or incompatible"):
        module.resume_step_from_manifest(
            manifest,
            asof="2026-08-24",
            valid_step_ids=valid_steps,
            config_sha256="changed",
            orchestrator_source_sha256="source",
        )


def test_resume_rewinds_read_only_validator_to_local_producer(
    tmp_path: Path,
) -> None:
    module = _module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "acceptance": "FAIL",
                "asof_date": "2026-08-24",
                "orchestrator_version": module.ORCHESTRATOR_VERSION,
                "config_sha256": "config",
                "orchestrator_source_sha256": "source",
                "failed_step_ids": ["08a_validate_metrics"],
            }
        ),
        encoding="utf-8",
    )
    valid_steps = [
        step.step_id for step in module.build_steps("2026-08-24")
    ]
    assert module.resume_step_from_manifest(
        manifest,
        asof="2026-08-24",
        valid_step_ids=valid_steps,
        config_sha256="config",
        orchestrator_source_sha256="source",
    ) == "19_build_exact_pit"
