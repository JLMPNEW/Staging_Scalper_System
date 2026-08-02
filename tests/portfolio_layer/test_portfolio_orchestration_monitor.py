from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_orchestrator() -> ModuleType:
    path = (
        PROJECT_ROOT
        / "portfolio_layer"
        / "orchestration"
        / "18_run_portfolio_pipeline.py"
    )
    spec = importlib.util.spec_from_file_location(
        "portfolio_daily_orchestrator_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_monitor_stable_manifest_verifies_parent_outputs_and_children(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    run_dir = tmp_path / "2026-07-31"
    stable = run_dir / "expectations_monitor" / "daily_monitor_manifest.json"
    session = tmp_path / "session"
    output = session / "daily_monitor_steps.csv"
    child = session / "child_manifest.json"
    parent = session / "daily_monitor_manifest.json"

    output.parent.mkdir(parents=True)
    output.write_text("status\nPASS\n", encoding="utf-8")
    orchestrator.write_manifest(child, {"acceptance": "PASS"})
    orchestrator.write_manifest(
        parent,
        {
            "acceptance": "PASS_WITH_DEFERRED",
            "as_of_date": "2026-07-31",
            "outputs_sha256": {output.name: orchestrator.sha256_file(output)},
            "child_manifests": [
                {
                    "manifest_path": str(child.resolve()),
                    "manifest_sha256": orchestrator.sha256_file(child),
                }
            ],
        },
    )
    orchestrator.write_manifest(
        stable,
        {
            "acceptance": "PASS_WITH_DEFERRED",
            "run_as_of": "2026-07-31",
            "parent_manifest_path": str(parent.resolve()),
            "parent_manifest_sha256": orchestrator.sha256_file(parent),
        },
    )

    assert (
        orchestrator.manifest_acceptance(
            run_dir,
            "expectations_monitor/daily_monitor_manifest.json",
        )
        == "PASS_WITH_DEFERRED"
    )

    child.write_text('{"acceptance": "FAIL"}', encoding="utf-8")
    assert orchestrator.manifest_acceptance(
        run_dir,
        "expectations_monitor/daily_monitor_manifest.json",
    ).startswith("STALE_PARENT_CHILD:")


def test_monitor_filter_is_mandatory_second_pass() -> None:
    orchestrator = _load_orchestrator()
    for cadence in ("tactical", "strategic"):
        groups = orchestrator.DEFAULT_CADENCES[cadence]
        assert groups.index("monitor") < groups.index("monitor_filter")
        post_filter = groups.index("monitor_filter")
        assert post_filter < groups.index("rotation", post_filter)
        assert post_filter < groups.index("macro_contract")
        assert groups.index("monitor_filter") < len(groups) - 1 - groups[::-1].index("final")
        assert len(groups) - 1 - groups[::-1].index("final") < groups.index("final_report")
    assert "monitor" not in orchestrator.SOFT_GROUPS

    config = orchestrator.load_yaml(
        PROJECT_ROOT / "portfolio_layer" / "config.yaml"
    )
    configured = config["orchestration"]["cadences"]
    for cadence in ("tactical", "strategic"):
        groups = configured[cadence]
        assert groups.index("monitor") < groups.index("monitor_filter")
        post_filter = groups.index("monitor_filter")
        assert post_filter < groups.index("rotation", post_filter)
        assert post_filter < groups.index("macro_contract")
        final_index = len(groups) - 1 - groups[::-1].index("final")
        assert groups.index("monitor_filter") < final_index
        assert final_index < groups.index("final_report")

    args = SimpleNamespace(force=True, reuse_risk_price_data=False)
    bootstrap = orchestrator.script_args(
        args,
        "09_run_portfolio_optimizer.py",
        group="optimizer",
    )
    deployable = orchestrator.script_args(
        args,
        "09_run_portfolio_optimizer.py",
        group="monitor_filter",
    )
    assert bootstrap[-2:] == ["--monitor-overlay-mode", "ignore"]
    assert deployable[-2:] == ["--monitor-overlay-mode", "required"]

    bootstrap_final = orchestrator.script_args(
        args,
        "20_compose_final_target_book.py",
        group="bootstrap_final",
    )
    deployable_final = orchestrator.script_args(
        args,
        "20_compose_final_target_book.py",
        group="final",
    )
    assert "--monitor-bootstrap" in bootstrap_final
    assert "--monitor-bootstrap" not in deployable_final
    bootstrap_group = orchestrator.GROUPS["bootstrap_final"]
    assert bootstrap_group[0][2] == "final/bootstrap_final_weights_manifest.json"


def test_historical_catchup_suppresses_current_provider_event_cycle() -> None:
    orchestrator = _load_orchestrator()
    args = SimpleNamespace(
        force=False,
        reuse_risk_price_data=False,
        historical_catchup=True,
    )
    assert orchestrator.script_args(
        args,
        "50_run_expectations_monitor_daily.py",
        group="monitor",
    ) == ["--skip-event-cycle"]
