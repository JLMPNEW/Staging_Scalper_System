from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace

import pytest


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


def _load_monitor_universe() -> ModuleType:
    path = (
        PROJECT_ROOT
        / "portfolio_layer"
        / "expectations_monitor"
        / "39_sync_monitor_universe.py"
    )
    spec = importlib.util.spec_from_file_location(
        "monitor_universe_sync_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_expectations_state() -> ModuleType:
    path = (
        PROJECT_ROOT
        / "portfolio_layer"
        / "expectations_monitor"
        / "56_build_expectations_state.py"
    )
    spec = importlib.util.spec_from_file_location(
        "expectations_state_blank_market_test",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_expectations_escalations_accept_blank_optional_market_evidence() -> None:
    module = _load_expectations_state()
    event = {
        "event_type": "guidance_cut",
        "severity": 4.5,
        "event_id": "event-1",
        "direction": -1,
        "material_flag": 1,
    }

    flags, floor, evidence = module._escalations(
        [event],
        {"abnormal_ret_1d_z": "", "rel_ret_5d": "", "rel_ret_20d": ""},
    )

    assert "R1" in flags
    assert "R4" not in flags
    assert floor == "watch"
    assert evidence == ["event-1"]


def _write_sealed_ledger(
    orchestrator: ModuleType,
    run_dir: Path,
    *,
    ticker: str = "AAA",
) -> Path:
    ledger_dir = run_dir / "ledger"
    ledger_dir.mkdir(parents=True)
    positions = ledger_dir / "broker_net_stock_positions.csv"
    positions.write_text(
        "symbol,net_shares,shares_lent\n" f"{ticker},10,0\n",
        encoding="utf-8",
    )
    orchestrator.write_manifest(
        ledger_dir / "ledger_manifest.json",
        {
            "acceptance": "PASS",
            "run_as_of": run_dir.name,
            "provenance_sha256": {
                "broker_net_stock_positions": orchestrator.sha256_file(positions)
            },
        },
    )
    return positions


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
        # Ledger-before-monitor is NOT a static cadence property: tactical has no
        # ledger pass and strategic lists ledger after monitor. When holdings are
        # required, plan_groups() normalizes one ledger pass before the first
        # monitor group (covered by the dedicated plan_groups tests below).
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


def test_monitor_filter_partial_recovery_is_intrinsically_rebuilt(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    plan = orchestrator.build_step_plan(
        ["monitor_filter"],
        orchestrator.GROUPS,
    )
    steps = plan[0]

    assert all(step["occurrence"] == 1 for step in steps)
    assert all(step["self_force"] for step in steps)
    assert all(
        not orchestrator.step_resume_skip(
            tmp_path, step, operator_force=False
        )
        for step in steps
    )


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
    assert orchestrator.script_args(
        args,
        "37_sync_earnings_dates.py",
        group="earnings",
    ) == ["--historical-catchup"]


def test_liquidity_attempt_precedes_authoritative_risk_gates() -> None:
    orchestrator = _load_orchestrator()
    scripts = [script for _subdir, script, _manifest in orchestrator.GROUPS["risk"]]
    collector = scripts.index("05c_collect_ib_historical_spread_samples.py")
    audit = scripts.index("05d_audit_liquidity_panel.py")
    validator = scripts.index("08_validate_risk_panel.py")
    assert collector < audit < validator
    assert "05c_collect_ib_historical_spread_samples.py" in (
        orchestrator.OPTIONAL_STEP_SCRIPTS
    )


def test_missing_ledger_fails_fast_for_holdings_required_monitor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: False,
    )
    args = SimpleNamespace(
        groups="ledger,exits,monitor,monitor_filter,final",
        cadence="strategic",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
    }

    with pytest.raises(ValueError, match="requires same-date broker holdings"):
        orchestrator.plan_groups(args, config, tmp_path / "2026-08-03")


def test_missing_statement_uses_prior_ledger_and_defers_only_current_broker_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: False,
    )
    runs_root = tmp_path / "runs"
    _write_sealed_ledger(orchestrator, runs_root / "2026-08-02")
    args = SimpleNamespace(
        groups=(
            "ledger,exits,payout,monitor,monitor_filter,final,final_report"
        ),
        cadence="strategic",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
        "holdings_ledger": {
            "max_staleness_days": 7,
            "missing_same_date_statement_policy": (
                orchestrator.DEFERRED_LEDGER_POLICY
            ),
        },
    }

    planned, metadata = orchestrator.plan_groups_with_metadata(
        args,
        config,
        runs_root / "2026-08-03",
    )

    assert planned == ["macro_contract", "monitor", "monitor_filter", "final", "final_report"]
    assert metadata["deferred_groups"] == ["ledger", "exits", "payout"]
    assert metadata["broker_holdings_source_as_of"] == "2026-08-02"
    assert metadata["broker_holdings_age_days"] == 1
    assert not metadata["same_date_statement_available"]
    assert not metadata["same_date_ledger_available"]


def test_prior_ledger_fallback_rejects_hash_tampering_and_staleness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: False,
    )
    runs_root = tmp_path / "runs"
    positions = _write_sealed_ledger(
        orchestrator,
        runs_root / "2026-08-02",
    )
    args = SimpleNamespace(
        groups="monitor,monitor_filter,final",
        cadence="tactical",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
        "holdings_ledger": {
            "max_staleness_days": 0,
            "missing_same_date_statement_policy": (
                orchestrator.DEFERRED_LEDGER_POLICY
            ),
        },
    }

    with pytest.raises(ValueError, match="no bounded hash-verified prior ledger"):
        orchestrator.plan_groups(args, config, runs_root / "2026-08-03")

    config["holdings_ledger"]["max_staleness_days"] = 7
    positions.write_text(
        "symbol,net_shares,shares_lent\nAAA,999,0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no bounded hash-verified prior ledger"):
        orchestrator.plan_groups(args, config, runs_root / "2026-08-03")


def test_monitor_universe_consumes_bounded_prior_ledger(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    monitor = _load_monitor_universe()
    runs_root = tmp_path / "runs"
    _write_sealed_ledger(
        orchestrator,
        runs_root / "2026-08-02",
        ticker="HELD",
    )
    config = {
        "holdings_ledger": {
            "max_staleness_days": 7,
            "missing_same_date_statement_policy": (
                monitor.DEFERRED_LEDGER_POLICY
            ),
        }
    }

    rows, sources, dependency = monitor._broker_holdings_source(
        config=config,
        run_dir=runs_root / "2026-08-03",
        run_as_of="2026-08-03",
        required=True,
    )

    assert [row["symbol"] for row in rows] == ["HELD"]
    assert sources[0]["source_run_as_of"] == "2026-08-02"
    assert sources[0]["consumer_run_as_of"] == "2026-08-03"
    assert dependency["status"] == "DEFERRED_SAME_DATE"
    assert dependency["source_as_of"] == "2026-08-02"
    assert dependency["age_days"] == 1


def test_portfolio_runtime_handoff_is_explicit_and_fail_closed(
    tmp_path: Path,
) -> None:
    orchestrator = _load_orchestrator()
    configured = tmp_path / "configured-python.exe"
    configured.write_bytes(b"runtime")
    current = tmp_path / "current-python.exe"
    current.write_bytes(b"runtime")
    config = {
        "orchestration": {"python_executable": str(configured)}
    }

    command = orchestrator.configured_runtime_command(
        config,
        argv=["--as-of", "2026-08-07"],
        current_executable=current,
    )

    assert command is not None
    assert Path(command[0]).resolve() == configured.resolve()
    assert command[2:] == ["--as-of", "2026-08-07"]
    assert (
        orchestrator.configured_runtime_command(
            config,
            argv=[],
            current_executable=configured,
        )
        is None
    )
    config["orchestration"]["python_executable"] = str(tmp_path / "missing.exe")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        orchestrator.configured_runtime_command(
            config,
            argv=[],
            current_executable=current,
        )


def test_holdings_required_monitor_normalizes_ledger_before_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: True,
    )
    args = SimpleNamespace(
        groups="monitor,monitor_filter,ledger,final",
        cadence="strategic",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
    }

    planned = orchestrator.plan_groups(
        args,
        config,
        tmp_path / "2026-08-03",
    )

    assert planned == ["ledger", "macro_contract", "monitor", "monitor_filter", "final"]


@pytest.mark.parametrize("ledger_ready", [False, True])
def test_holdings_required_monitor_normalizes_ledger_before_bootstrap_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_ready: bool,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        orchestrator,
        "_sealed_ledger_available",
        lambda _run_dir: ledger_ready,
    )
    args = SimpleNamespace(
        groups="bootstrap_final,earnings,monitor,monitor_filter,ledger,final",
        cadence="strategic",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
    }

    planned = orchestrator.plan_groups(
        args,
        config,
        tmp_path / "2026-08-17",
    )

    assert planned == [
        "ledger",
        "macro_contract",
        "bootstrap_final",
        "earnings",
        "monitor",
        "monitor_filter",
        "final",
    ]


def test_default_run_as_of_rejects_stale_latest_run_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import date

    orchestrator = _load_orchestrator()
    # Hermetic calendar: 2026-08-03 is a regular Monday session.
    monkeypatch.setattr(
        orchestrator,
        "_previous_nyse_trading_day",
        lambda today: today,
    )
    today = date(2026, 8, 3)
    runs_root = tmp_path / "runs"
    friday = runs_root / "2026-07-31"
    friday.mkdir(parents=True)
    (friday / "stocks_scores.csv").write_text("ticker\n", encoding="utf-8")

    # A bare default may not resume a prior session: self-forced re-pass steps
    # would rebuild Friday's sealed final book without operator --force.
    with pytest.raises(ValueError, match=r"Pass --as-of 2026-08-03"):
        orchestrator.default_run_as_of(runs_root, today=today)

    # Resuming the current session is allowed.
    monday = runs_root / "2026-08-03"
    monday.mkdir()
    (monday / "stocks_scores.csv").write_text("ticker\n", encoding="utf-8")
    assert orchestrator.default_run_as_of(runs_root, today=today) == "2026-08-03"

    # A fresh runs root defaults to the current calendar session.
    assert (
        orchestrator.default_run_as_of(tmp_path / "empty", today=today)
        == "2026-08-03"
    )


def test_holdings_required_monitor_rejects_explicit_ledger_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: True,
    )
    args = SimpleNamespace(
        groups="ledger,monitor,monitor_filter,final",
        cadence="strategic",
        skip="ledger",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": True}
        },
    }

    with pytest.raises(ValueError, match="explicitly skipped"):
        orchestrator.plan_groups(args, config, tmp_path / "2026-08-03")

def test_monitor_keeps_post_filter_macro_contract_and_adds_prerequisite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = _load_orchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_broker_statement_available",
        lambda *_args, **_kwargs: True,
    )
    args = SimpleNamespace(
        groups="macro_contract,monitor,monitor_filter,macro_contract,final",
        cadence="strategic",
        skip="",
        config=tmp_path / "config.yaml",
    )
    config = {
        "orchestration": {"cadences": {}},
        "expectations_monitor": {
            "universe": {"require_broker_holdings": False}
        },
    }

    planned = orchestrator.plan_groups(
        args,
        config,
        tmp_path / "2026-08-18",
    )

    assert planned == [
        "macro_contract",
        "monitor",
        "monitor_filter",
        "macro_contract",
        "final",
    ]
