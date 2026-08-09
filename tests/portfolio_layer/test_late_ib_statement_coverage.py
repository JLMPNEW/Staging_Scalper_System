from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reconciler() -> ModuleType:
    return _load(
        PROJECT_ROOT / "orchestration" / "reconcile_late_ib_statements.py",
        "late_ib_reconciler_test",
    )


def _earnings_sync() -> ModuleType:
    return _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "earnings_dates"
        / "37_sync_earnings_dates.py",
        "earnings_sync_test",
    )


def _level_outcomes() -> ModuleType:
    return _load(
        PROJECT_ROOT / "portfolio_layer" / "levels" / "63_update_level_outcomes.py",
        "level_outcomes_test",
    )


def _portfolio_runner() -> ModuleType:
    return _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "orchestration"
        / "18_run_portfolio_pipeline.py",
        "portfolio_runner_late_supplement_test",
    )


def _write_sealed_csv(
    module: ModuleType,
    *,
    path: Path,
    manifest_path: Path,
    run_as_of: str,
    content: str,
    output_key: str,
    provenance: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    hashes_key = "provenance_sha256" if provenance else "outputs_sha256"
    hash_name = Path(output_key).stem if provenance else output_key
    module.write_manifest(
        manifest_path,
        {
            "acceptance": "PASS",
            "run_as_of": run_as_of,
            hashes_key: {hash_name: module.sha256_file(path)},
        },
    )


def _coverage_run(tmp_path: Path) -> tuple[ModuleType, Path]:
    module = _reconciler()
    run_as_of = "2026-08-07"
    run_dir = tmp_path / run_as_of
    _write_sealed_csv(
        module,
        path=run_dir / "ledger" / "broker_net_stock_positions.csv",
        manifest_path=run_dir / "ledger" / "ledger_manifest.json",
        run_as_of=run_as_of,
        content="symbol,net_shares\nVST,50\nSEZL,40\n",
        output_key="broker_net_stock_positions.csv",
        provenance=True,
    )
    _write_sealed_csv(
        module,
        path=run_dir / "earnings_dates" / "earnings_calendar.csv",
        manifest_path=run_dir / "earnings_dates" / "earnings_manifest.json",
        run_as_of=run_as_of,
        content="ticker,next_earnings_date\nVST,\n",
        output_key="earnings_calendar.csv",
    )
    _write_sealed_csv(
        module,
        path=run_dir / "expectations_monitor" / "monitor_universe.csv",
        manifest_path=run_dir
        / "expectations_monitor"
        / "monitor_universe_manifest.json",
        run_as_of=run_as_of,
        content="ticker,is_holding\nVST,1\n",
        output_key="monitor_universe.csv",
    )
    return module, run_dir


def test_late_holding_missing_from_coverage_triggers_refresh(tmp_path: Path) -> None:
    module, run_dir = _coverage_run(tmp_path)

    gaps = module.late_holding_coverage_gaps(run_dir, run_dir.name)

    assert gaps["holdings"] == ["SEZL", "VST"]
    assert gaps["missing_from_earnings"] == ["SEZL"]
    assert gaps["missing_from_monitor"] == ["SEZL"]
    groups = list(module.COVERAGE_POST_LEDGER_GROUPS)
    assert groups.index("bootstrap_final") < groups.index("earnings")
    assert groups.index("earnings") < groups.index("monitor")
    assert groups.index("monitor") < groups.index("monitor_filter")
    assert groups.index("monitor_filter") < groups.index("final_report")
    command = module.reconciliation_command(
        Path("config.yaml"),
        "2026-08-07",
        "2026-08-07",
        groups=module.COVERAGE_POST_LEDGER_GROUPS,
        meta_name=module.RECOVERY_META_NAME,
        late_holding_supplement=True,
    )
    assert "--late-holding-supplement" in command


def test_late_holding_coverage_fails_closed_on_tampered_artifact(
    tmp_path: Path,
) -> None:
    module, run_dir = _coverage_run(tmp_path)
    with (run_dir / "earnings_dates" / "earnings_calendar.csv").open(
        "a", encoding="utf-8"
    ) as handle:
        handle.write("SEZL,2099-01-01\n")

    with pytest.raises(ValueError, match="hash mismatch"):
        module.late_holding_coverage_gaps(run_dir, run_dir.name)


def test_earnings_fallback_prioritizes_holdings_before_candidates() -> None:
    module = _earnings_sync()
    universe = {
        "AAA": {
            "ticker": "AAA",
            "is_holding": "0",
            "investable_eligible": "1",
        },
        "ZZZ": {
            "ticker": "ZZZ",
            "is_holding": "1",
            "investable_eligible": "0",
        },
        "BBB": {
            "ticker": "BBB",
            "is_holding": "0",
            "investable_eligible": "0",
        },
    }

    ordered = module.order_universe_entries(universe)

    assert [row["ticker"] for row in ordered] == ["ZZZ", "AAA", "BBB"]


def test_preserved_level_drift_is_deferred_only_in_explicit_supplement_mode() -> None:
    module = _level_outcomes()
    base = {
        "chain_errors": [],
        "resolution_errors": [],
        "retirement_errors": [],
        "first_write_drifts": 1,
        "deferred": False,
    }

    assert module._outcome_acceptance(**base) == "FAIL"
    assert (
        module._outcome_acceptance(
            **base,
            preserve_drifts_as_deferred=True,
        )
        == "PASS_WITH_DEFERRED"
    )
    assert (
        module._outcome_acceptance(
            **{**base, "chain_errors": ["tampered"]},
            preserve_drifts_as_deferred=True,
        )
        == "FAIL"
    )


def test_portfolio_runner_passes_supplement_flag_only_to_monitor_driver() -> None:
    module = _portfolio_runner()
    args = SimpleNamespace(
        force=True,
        reuse_risk_price_data=False,
        historical_catchup=False,
        late_holding_supplement=True,
    )

    monitor_flags = module.script_args(
        args,
        "50_run_expectations_monitor_daily.py",
        group="monitor",
    )
    optimizer_flags = module.script_args(
        args,
        "09_run_portfolio_optimizer.py",
        group="monitor_filter",
    )

    assert "--late-holding-supplement" in monitor_flags
    assert "--late-holding-supplement" not in optimizer_flags
