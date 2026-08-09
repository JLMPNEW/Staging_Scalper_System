from __future__ import annotations

import csv
import base64
import json
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

import factor_validation.artifacts as artifact_module
from factor_validation import (
    ProvenanceFileSet,
    campaign_ledger_path,
    canonical_json_bytes,
    evidence_package_path,
    load_campaign_registry,
    read_campaign_ledger,
    sha256_bytes,
    sha256_file,
    verify_campaign_ledger,
)
import technology.adapters.factor_validation_shadow as shadow_adapter
from technology.adapters.factor_validation_shadow import (
    TechnologyShadowSettings,
    run_technology_factor_validation_shadow,
    settings_from_config,
    validate_technology_factor_validation_shadow,
)
from technology.core.signal_diagnostics import (
    forward_return_observation_is_usable,
    maximum_forward_label_staleness_days,
    missing_required_historical_membership,
    spearman,
)


FACTORS = ("gross_margin", "realized_vol_60d")
HORIZONS = (21, 63)


def test_forward_return_eligibility_never_filters_finite_outcomes_by_magnitude() -> None:
    assert forward_return_observation_is_usable(-2.0, 0.1, 1.0)
    assert forward_return_observation_is_usable(8.0, -0.2, -1.5)
    assert not forward_return_observation_is_usable(float("inf"), 0.1, 1.0)
    assert not forward_return_observation_is_usable(0.1, None, 1.0)
    assert missing_required_historical_membership(
        include_inactive=True,
        membership_ticker_count=0,
    )
    assert not missing_required_historical_membership(
        include_inactive=False,
        membership_ticker_count=0,
    )
    assert maximum_forward_label_staleness_days((21, 63), 21) == 132


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _fixture_settings(tmp_path: Path) -> TechnologyShadowSettings:
    input_dir = tmp_path / "inputs"
    panel_path = input_dir / "signal_panel.csv"
    ic_path = input_dir / "subfeature_ic.csv"
    summary_path = input_dir / "stage8a_summary.json"
    config_path = input_dir / "config.yaml"
    dates = tuple(date(2023, 1, 3) + timedelta(days=28 * index) for index in range(16))
    panel_rows: list[dict[str, object]] = []
    per_cell: dict[tuple[str, int], list[float]] = {
        (factor, horizon): [] for factor in FACTORS for horizon in HORIZONS
    }
    for date_index, as_of in enumerate(dates):
        values: dict[str, list[float]] = {factor: [] for factor in FACTORS}
        returns: dict[int, list[float]] = {horizon: [] for horizon in HORIZONS}
        for entity_index in range(12):
            gross = float(entity_index) + date_index / 100
            volatility = 50.0 - float(entity_index) + date_index / 100
            return_21 = float(entity_index) + (
                (((entity_index + 1) * (date_index + 3)) % 7) - 3
            ) * 0.8
            return_63 = float(entity_index) + (
                (((entity_index + 2) * (date_index + 5)) % 9) - 4
            ) * 0.7
            panel_rows.append(
                {
                    "asof_date": as_of.isoformat(),
                    "ticker": f"T{entity_index:02d}",
                    "market_regime": "risk_on" if date_index % 2 == 0 else "risk_off",
                    "gross_margin": gross,
                    "realized_vol_60d": volatility,
                    "fwd_resid_21d": return_21,
                    "fwd_resid_63d": return_63,
                }
            )
            values["gross_margin"].append(gross)
            values["realized_vol_60d"].append(-volatility)
            returns[21].append(return_21)
            returns[63].append(return_63)
        for factor in FACTORS:
            for horizon in HORIZONS:
                value = spearman(values[factor], returns[horizon])
                assert value is not None
                per_cell[(factor, horizon)].append(value)
    _write_csv(panel_path, panel_rows)
    legacy_rows = []
    for factor in FACTORS:
        for horizon in HORIZONS:
            series = per_cell[(factor, horizon)]
            legacy_rows.append(
                {
                    "signal": factor,
                    "group": "test",
                    "n_dates": len(series),
                    "mean_ic": round(sum(series) / len(series), 4),
                    "horizon_days": horizon,
                }
            )
    _write_csv(ic_path, legacy_rows)
    summary_path.write_text(
        json.dumps(
            {
                "end_date": dates[-1].isoformat(),
                "horizons_trading_days": list(HORIZONS),
                "model_family": "software_infrastructure",
                "panel_dates": len(dates),
            "forward_return_filter_mode": "nonfinite_only",
            "forward_return_outlier_observation_counts": {"21": 0, "63": 0},
            "regime_method": "trailing_126d_benchmark_return_sign",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "software_infrastructure_calibrated_scoring": {
                    "component_weights": {"quality": 0.5, "risk_control": 0.5},
                    "subfeature_weights": {
                        "quality": {"gross_margin_score": 1.0},
                        "risk_control": {"realized_vol_60d_score": 1.0},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return TechnologyShadowSettings(
        config_path=config_path,
        model_family="software_infrastructure",
        family_id="software_shadow_test_v1",
        signal_panel_path=panel_path,
        legacy_ic_path=ic_path,
        legacy_summary_path=summary_path,
        output_root=tmp_path / "evidence",
        factor_ids=FACTORS,
        horizons_trading_days=HORIZONS,
        evaluation_step_trading_days=20,
        min_cross_section=8,
        min_dates=12,
        min_independent_windows=3,
        min_regime_dates=3,
        quantile_count=4,
        min_extreme_bucket_size=2,
    )


def test_shadow_pilot_reconciles_publishes_and_reruns_idempotently(
    tmp_path: Path,
) -> None:
    settings = _fixture_settings(tmp_path)
    inputs = (
        settings.config_path,
        settings.signal_panel_path,
        settings.legacy_ic_path,
        settings.legacy_summary_path,
    )
    input_hashes = {path: sha256_file(path) for path in inputs}

    first = run_technology_factor_validation_shadow(settings)

    assert first["evidence_package_count"] == 4
    assert first["legacy_authoritative"] is True
    assert first["portfolio_impact"] is False
    assert first["portfolio_write_enabled"] is False
    assert first["production_promotion_enabled"] is False
    assert first["prospective_claim_authorized"] is False
    assert first["selection_design"] == "retrospective_full_family"
    assert first["entry_lag_trading_days"] == 0
    assert first["round_trip_cost"] == pytest.approx(0.003)
    assert first["shared_gate_active"] is False
    assert first["reused_existing_packages"] is False
    assert all(
        cell["max_abs_per_date_ic_difference"] <= 1e-12 for cell in first["cells"]
    )
    low_vol_cells = [
        cell for cell in first["cells"] if cell["factor_id"] == "realized_vol_60d"
    ]
    assert all(cell["shared_mean_ic"] < 0 for cell in low_vol_cells)
    assert all(
        cell["shared_direction_adjusted_mean_ic"] > 0 for cell in low_vol_cells
    )
    assert all(cell["production_active"] is True for cell in first["cells"])
    registry = load_campaign_registry(
        settings.output_root / first["campaign_id"] / "campaign_registry.json"
    )
    assert all(cell.sector_id == "software_infrastructure" for cell in registry.cells)
    assert {
        seal.logical_path for seal in registry.cells[0].code_files
    } >= {"factor_validation/evidence.py"}
    report_events = [
        entry
        for entry in read_campaign_ledger(settings.output_root)
        if entry["event_type"] == "campaign_report_published"
    ]
    assert len(report_events) == 1
    assert first["sealed_code_snapshot"]["file_count"] == len(
        registry.cells[0].code_files
    )
    assert first["sealed_code_vcs"]["reproducibility_status"] in {
        "git_head_reproduces_sealed_code",
        "exact_bytes_recoverable_from_anchored_snapshot",
    }
    snapshot_path = (
        settings.output_root
        / first["campaign_id"]
        / first["sealed_code_snapshot"]["path"]
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot_seals = {
        (
            item["logical_path"],
            sha256_bytes(base64.b64decode(item["content_base64"], validate=True)),
            len(base64.b64decode(item["content_base64"], validate=True)),
        )
        for item in snapshot["files"]
    }
    assert snapshot_seals == {
        (seal.logical_path, seal.sha256, seal.size_bytes)
        for seal in registry.cells[0].code_files
    }
    first_summary = json.loads(
        (
            evidence_package_path(
                settings.output_root,
                registry,
                cell_id=registry.cells[0].cell_id,
            )
            / "summary.json"
        ).read_text(encoding="utf-8")
    )
    assert {item["regime"] for item in first_summary["regime_diagnostics"]} == {
        "risk_on",
        "risk_off",
    }
    for cell in registry.cells:
        if cell.factor_id != "realized_vol_60d":
            continue
        acceptance = json.loads(
            (
                evidence_package_path(
                    settings.output_root,
                    registry,
                    cell_id=cell.cell_id,
                )
                / "acceptance.json"
            ).read_text(encoding="utf-8")
        )
        gate_map = {item["name"]: item["passed"] for item in acceptance["gates"]}
        assert gate_map["factor_direction_consistent"] is True
    validation = validate_technology_factor_validation_shadow(
        settings.output_root,
        campaign_id=first["campaign_id"],
    )
    assert validation["ok"] is True
    assert validation["errors"] == []

    ledger_before = campaign_ledger_path(settings.output_root).read_bytes()
    reconciliation_before = Path(first["reconciliation_path"]).read_bytes()
    second = run_technology_factor_validation_shadow(settings)

    assert second["campaign_id"] == first["campaign_id"]
    assert second["reused_existing_packages"] is True
    assert campaign_ledger_path(settings.output_root).read_bytes() == ledger_before
    assert Path(second["reconciliation_path"]).read_bytes() == reconciliation_before
    assert {path: sha256_file(path) for path in inputs} == input_hashes


def test_shadow_pilot_fails_before_publication_on_legacy_mismatch(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    rows: list[dict[str, object]]
    with settings.legacy_ic_path.open("r", encoding="utf-8", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]
    rows[0]["mean_ic"] = "0.9999"
    _write_csv(settings.legacy_ic_path, rows)

    with pytest.raises(ValueError, match="legacy mean_ic mismatch"):
        run_technology_factor_validation_shadow(settings)

    assert not settings.output_root.exists()


def test_shadow_pilot_refuses_a_cherry_picked_legacy_subset(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    subset = replace(settings, factor_ids=("gross_margin",))

    with pytest.raises(ValueError, match="full-family membership mismatch"):
        run_technology_factor_validation_shadow(subset)

    assert not settings.output_root.exists()


def test_new_sequential_look_requires_a_newer_panel_date(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    run_technology_factor_validation_shadow(settings)
    ledger_before = campaign_ledger_path(settings.output_root).read_bytes()
    config = json.loads(settings.config_path.read_text(encoding="utf-8"))
    config["software_infrastructure_calibrated_scoring"]["model_version"] = "changed"
    settings.config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="strictly newer panel date"):
        run_technology_factor_validation_shadow(settings)

    assert campaign_ledger_path(settings.output_root).read_bytes() == ledger_before


def test_same_panel_code_amendment_requires_byte_identical_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _fixture_settings(tmp_path)
    code_path = tmp_path / "sealed_code.py"
    code_path.write_text("VERSION = 1\n", encoding="utf-8")

    def local_provenance(current: TechnologyShadowSettings) -> ProvenanceFileSet:
        return ProvenanceFileSet(
            config_path=current.config_path,
            source_paths={
                "technology/signal_panel.csv": current.signal_panel_path,
                "technology/subfeature_ic.csv": current.legacy_ic_path,
                "technology/stage8a_summary.json": current.legacy_summary_path,
            },
            code_paths={"tests/sealed_code.py": code_path},
        )

    monkeypatch.setattr(shadow_adapter, "_provenance_files", local_provenance)
    first = run_technology_factor_validation_shadow(settings)
    code_path.write_text("VERSION = 2\n", encoding="utf-8")
    second = run_technology_factor_validation_shadow(settings)

    assert second["campaign_id"] != first["campaign_id"]
    assert second["reused_existing_packages"] is False
    assert second["sequential_testing"] == {
        "alpha_spending_method": "bonferroni_equal",
        "amends_campaign_id": first["campaign_id"],
        "code_only_amendment": True,
        "familywise_alpha": 0.05,
        "look_index": 1,
        "maximum_looks": 12,
        "methodology_amendment": False,
        "per_look_fdr_alpha": 0.05 / 12.0,
        "statistical_result_identity": "byte_exact_prior_evidence",
    }
    validation = validate_technology_factor_validation_shadow(
        settings.output_root,
        campaign_id=second["campaign_id"],
    )
    assert validation["ok"] is True

    ledger_before = campaign_ledger_path(settings.output_root).read_bytes()
    third = run_technology_factor_validation_shadow(settings)
    assert third["campaign_id"] == second["campaign_id"]
    assert third["reused_existing_packages"] is True
    assert campaign_ledger_path(settings.output_root).read_bytes() == ledger_before


def test_new_code_version_abandons_only_an_exact_interrupted_amendment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _fixture_settings(tmp_path)
    code_path = tmp_path / "sealed_code.py"
    code_path.write_text("VERSION = 1\n", encoding="utf-8")

    def local_provenance(current: TechnologyShadowSettings) -> ProvenanceFileSet:
        return ProvenanceFileSet(
            config_path=current.config_path,
            source_paths={
                "technology/signal_panel.csv": current.signal_panel_path,
                "technology/subfeature_ic.csv": current.legacy_ic_path,
                "technology/stage8a_summary.json": current.legacy_summary_path,
            },
            code_paths={"tests/sealed_code.py": code_path},
        )

    monkeypatch.setattr(shadow_adapter, "_provenance_files", local_provenance)
    completed = run_technology_factor_validation_shadow(settings)
    code_path.write_text("VERSION = 2\n", encoding="utf-8")
    real_publish = artifact_module._write_evidence_package
    calls = 0

    def stop_after_first(*args: Any, **call_kwargs: Any):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated process interruption")
        return real_publish(*args, **call_kwargs)

    monkeypatch.setattr(artifact_module, "_write_evidence_package", stop_after_first)
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        run_technology_factor_validation_shadow(settings)
    interrupted = verify_campaign_ledger(settings.output_root)
    assert interrupted.ok is False
    assert any("ledger_incomplete_published_family" in item for item in interrupted.errors)

    code_path.write_text("VERSION = 3\n", encoding="utf-8")
    monkeypatch.setattr(artifact_module, "_write_evidence_package", real_publish)
    recovered = run_technology_factor_validation_shadow(settings)
    assert recovered["sequential_testing"]["look_index"] == 1
    assert recovered["sequential_testing"]["amends_campaign_id"] == completed["campaign_id"]
    abandonment = [
        entry
        for entry in read_campaign_ledger(settings.output_root)
        if entry["event_type"] == "family_abandoned"
    ]
    assert len(abandonment) == 1
    assert abandonment[0]["error_code"] == "code_provenance_changed_after_interruption"
    assert verify_campaign_ledger(settings.output_root).ok is True


def test_config_cannot_enable_production_promotion(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    section = {
        "mode": "shadow",
        "production_promotion_enabled": True,
        "portfolio_write_enabled": False,
        "model_family": "software_infrastructure",
        "family_id": "test_family",
        "signal_panel_path": "panel.csv",
        "legacy_ic_path": "legacy.csv",
        "legacy_summary_path": "summary.json",
        "output_root": "evidence",
        "factor_ids": ["gross_margin"],
        "horizons_trading_days": [21],
        "evaluation_step_trading_days": 21,
        "round_trip_cost_source": "test_30bps",
        "cross_campaign_familywise_alpha": 0.05,
        "cross_campaign_max_looks": 12,
        "alpha_spending_method": "bonferroni_equal",
        "require_complete_legacy_family": True,
        "selection_design": "retrospective_full_family",
        "prospective_claim_authorized": False,
        "production_scoring_config_key": "software_infrastructure_calibrated_scoring",
        "methodology_amendment_id": "test_methodology_v1",
    }
    config_path.write_text(
        json.dumps({"technology_factor_validation_shadow": section}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="production_promotion_enabled must remain false"):
        settings_from_config(config_path)


def test_shadow_output_root_rejects_portfolio_paths(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    with pytest.raises(ValueError, match="must not be a portfolio path"):
        replace(
            settings,
            output_root=tmp_path / "portfolio_layer" / "factor_evidence",
        )


def test_same_panel_methodology_change_consumes_a_new_look(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    first = run_technology_factor_validation_shadow(settings)
    second = run_technology_factor_validation_shadow(
        replace(settings, methodology_amendment_id="test_methodology_v2")
    )
    assert second["campaign_id"] != first["campaign_id"]
    assert second["sequential_testing"]["look_index"] == 2
    assert second["sequential_testing"]["methodology_amendment"] is True
    assert second["sequential_testing"]["code_only_amendment"] is False
    assert second["sequential_testing"]["statistical_result_identity"] is None


def test_reconciliation_report_tampering_is_caught_by_ledger(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    result = run_technology_factor_validation_shadow(settings)
    report_path = Path(result["reconciliation_path"])
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["portfolio_impact"] = True
    report_path.write_bytes(canonical_json_bytes(payload))

    validation = validate_technology_factor_validation_shadow(
        settings.output_root,
        campaign_id=result["campaign_id"],
    )
    assert validation["ok"] is False
    assert any("ledger_report_hash_mismatch" in error for error in validation["errors"])


def test_sealed_code_snapshot_tampering_is_caught(tmp_path: Path) -> None:
    settings = _fixture_settings(tmp_path)
    result = run_technology_factor_validation_shadow(settings)
    snapshot_path = (
        settings.output_root
        / result["campaign_id"]
        / result["sealed_code_snapshot"]["path"]
    )
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["files"][0]["content_base64"] = base64.b64encode(b"tampered").decode(
        "ascii"
    )
    snapshot_path.write_bytes(canonical_json_bytes(snapshot))

    validation = validate_technology_factor_validation_shadow(
        settings.output_root,
        campaign_id=result["campaign_id"],
    )
    assert validation["ok"] is False
    assert "reconciliation_code_snapshot_hash_mismatch" in validation["errors"]
