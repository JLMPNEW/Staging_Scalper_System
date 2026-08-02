from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _state(
    ticker: str,
    internal_state: str,
    *,
    flags: str = "[]",
    market_status: str = "current",
) -> dict[str, str]:
    return {
        "ticker": ticker,
        "run_as_of": "2026-07-31",
        "investable_eligible": "1",
        "internal_state": internal_state,
        "action_state": "hold",
        "market_data_status": market_status,
        "escalation_flags_json": flags,
        "input_digest": ticker,
    }


def test_entry_policy_allows_only_clean_green_and_stable() -> None:
    module = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "optimizer"
        / "08_build_monitor_eligibility_overlay.py",
        "monitor_optimizer_overlay_test",
    )
    tickers = (
        "GREEN",
        "STABLE",
        "WATCH",
        "DETERIORATING",
        "BROKEN",
        "FLAGGED",
        "STALE",
        "MISSING",
    )
    scores = [
        {
            "ticker": ticker,
            "investable_eligible": "1",
            "source_pipeline": "test",
        }
        for ticker in tickers
    ]
    states = [
        _state("GREEN", "green"),
        _state("STABLE", "stable"),
        _state("WATCH", "watch"),
        _state("DETERIORATING", "deteriorating"),
        _state("BROKEN", "broken"),
        _state("FLAGGED", "stable", flags='["R6"]'),
        _state("STALE", "green", market_status="stale"),
    ]
    policy = {
        "entry_states": ["green", "stable"],
        "retention_states": ["green", "stable", "watch"],
        "blocking_escalation_flags": ["R6"],
        "minimum_investable_state_coverage_fraction": 0.75,
    }

    rows, checks = module.build_overlay_rows(
        scores,
        states,
        run_as_of="2026-07-31",
        policy=policy,
    )
    keyed = {row["ticker"]: row for row in rows}
    assert {
        ticker
        for ticker, row in keyed.items()
        if row["optimizer_entry_eligible"] == 1
    } == {"GREEN", "STABLE"}
    assert keyed["WATCH"]["optimizer_retention_eligible"] == 1
    assert keyed["MISSING"]["optimizer_retention_eligible"] == 1
    assert keyed["DETERIORATING"]["optimizer_retention_eligible"] == 0
    assert keyed["BROKEN"]["optimizer_retention_eligible"] == 0
    assert keyed["FLAGGED"]["policy_reason"] == "blocking_flags:R6"
    assert keyed["STALE"]["policy_reason"] == "market_data_not_current:stale"
    assert all(check["status"] == "PASS" for check in checks)


def test_final_composer_rejects_non_deployable_optimizer_lineage(
    tmp_path: Path,
) -> None:
    module = _load(
        PROJECT_ROOT
        / "portfolio_layer"
        / "orchestration"
        / "20_compose_final_target_book.py",
        "final_composer_monitor_lineage_test",
    )
    run_dir = tmp_path / "2026-07-31"
    optimizer_dir = run_dir / "optimizer"
    optimizer_dir.mkdir(parents=True)
    target = optimizer_dir / "target_weights.csv"
    overlay = optimizer_dir / "monitor_eligibility_overlay.csv"
    monitor_manifest_path = optimizer_dir / "monitor_eligibility_manifest.json"
    optimizer_manifest_path = optimizer_dir / "optimizer_manifest.json"
    target.write_text("ticker,weight\nABC,1.0\n", encoding="utf-8")
    overlay.write_text(
        "ticker,optimizer_entry_eligible\nABC,1\n",
        encoding="utf-8",
    )
    module.write_manifest(
        monitor_manifest_path,
        {
            "acceptance": "PASS",
            "run_as_of": "2026-07-31",
            "production_entry_gate": True,
            "policy": {"policy_version": "monitor_optimizer_entry_v1"},
            "outputs_sha256": {
                "monitor_eligibility_overlay.csv": module.sha256_file(overlay)
            },
        },
    )
    module.write_manifest(
        optimizer_manifest_path,
        {
            "acceptance": "PASS",
            "run_as_of": "2026-07-31",
            "deployable": True,
            "monitor_entry_policy": {"status": "applied"},
            "provenance_sha256": {
                "target_weights.csv": module.sha256_file(target),
                "monitor_eligibility_overlay.csv": module.sha256_file(overlay),
                "monitor_eligibility_manifest.json": module.sha256_file(
                    monitor_manifest_path
                ),
            },
        },
    )
    cost_manifest = {
        "provenance_sha256": {
            "target_weights.csv": module.sha256_file(target),
            "optimizer_manifest.json": module.sha256_file(
                optimizer_manifest_path
            ),
        }
    }
    result = module.require_monitor_filtered_aqr_lineage(
        run_dir,
        run_as_of="2026-07-31",
        cost_manifest=cost_manifest,
    )
    assert result["status"] == "applied"

    broken = module.read_manifest(optimizer_manifest_path)
    broken["deployable"] = False
    module.write_manifest(optimizer_manifest_path, broken)
    cost_manifest["provenance_sha256"]["optimizer_manifest.json"] = (
        module.sha256_file(optimizer_manifest_path)
    )
    with pytest.raises(ValueError, match="deployable"):
        module.require_monitor_filtered_aqr_lineage(
            run_dir,
            run_as_of="2026-07-31",
            cost_manifest=cost_manifest,
        )


def test_shared_seal_accepts_monitor_as_of_date(tmp_path: Path) -> None:
    from portfolio_layer.core import contracts
    artifact = tmp_path / "state.csv"
    artifact.write_text("ticker\nABC\n", encoding="utf-8")
    manifest = {
        "acceptance": "PASS",
        "as_of_date": "2026-07-31",
        "outputs_sha256": {
            "state.csv": contracts.sha256_file(artifact),
        },
    }
    assert contracts.sealed_artifact_errors(
        manifest,
        artifact,
        "state.csv",
        run_as_of="2026-07-31",
    ) == []
