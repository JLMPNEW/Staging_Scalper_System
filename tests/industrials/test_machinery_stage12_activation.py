from __future__ import annotations

import csv
import json
import os
import runpy
from datetime import date, datetime
from pathlib import Path

import pytest
import yaml

from industrials.machinery.scoring import file_sha256, survivorship_sidecar
from industrials.machinery.stage12_activation import (
    ACTIVATION_STATUS_FULLY_VALIDATED,
    ActivationPaths,
    _activation_date_checks,
    _capture_dashboard_state,
    _restore_dashboard_state,
    apply_active_production_policy,
    production_policy_source_hashes,
    rollback_published_candidate,
)
from industrials.machinery.stage12_contract_upgrade import (
    validate_active_portfolio_contract,
)
from industrials.machinery.stage12_activation_transaction import (
    PORTFOLIO_COMPLETION_ALIASES,
    ActivationOrchestrationLock,
    CommandResult,
    PORTFOLIO_RESUME_GROUPS,
    REQUIRED_PORTFOLIO_GROUPS,
    REUSABLE_PORTFOLIO_PREFIX_GROUPS,
    render_portfolio_activation_config,
    resolve_stage12_python,
    run_activation_transaction,
    validate_completed_session,
    validate_portfolio_smoke,
    validate_wall_clock,
)
from industrials.machinery.stage12_governance import (
    ACTIVATION_MODE_REPLACE_ACTIVE,
    machinery_portfolio_policy_fingerprint,
    portfolio_activation_fingerprint,
)


def _portfolio_config() -> dict:
    return {
        "score_contract": {
            "sectors": [
                {
                    "model_family": "machinery",
                    "required": False,
                    "enabled": True,
                }
            ]
        },
        "optimizer": {
            "sector_weight_caps": {"machinery": 0.0, "defense": 0.05},
            "fixed_equal_weight_sleeves": ["machinery"],
        },
    }


def test_contract_upgrade_allows_unrelated_sleeve_changes() -> None:
    config = _portfolio_config()
    machinery = config["score_contract"]["sectors"][0]
    machinery.update(
        {
            "required": True,
            "require_oos_score_valid": True,
        }
    )
    config["optimizer"]["sector_weight_caps"]["machinery"] = 0.05
    config["optimizer"]["sector_weight_caps"]["defense"] = 0.10

    expected_policy = machinery_portfolio_policy_fingerprint(config)
    validate_active_portfolio_contract(
        config,
        expected_cap=0.05,
        expected_policy_sha256=expected_policy,
    )

    machinery["required"] = False
    with pytest.raises(ValueError, match="activation contract"):
        validate_active_portfolio_contract(
            config,
            expected_cap=0.05,
            expected_policy_sha256=expected_policy,
        )


def test_contract_upgrade_rejects_machinery_policy_drift() -> None:
    config = _portfolio_config()
    machinery = config["score_contract"]["sectors"][0]
    machinery.update({"required": True, "require_oos_score_valid": True})
    config["optimizer"]["sector_weight_caps"]["machinery"] = 0.05
    expected_policy = machinery_portfolio_policy_fingerprint(config)

    config.setdefault("black_litterman_fusion", {}).setdefault("strategic_sector_weights", {})["machinery"] = 0.05

    with pytest.raises(ValueError, match="activation contract"):
        validate_active_portfolio_contract(
            config,
            expected_cap=0.05,
            expected_policy_sha256=expected_policy,
        )


def test_activation_fingerprint_allows_only_activation_settings() -> None:
    shadow = _portfolio_config()
    active = _portfolio_config()
    active["score_contract"]["sectors"][0]["required"] = True
    active["optimizer"]["sector_weight_caps"]["machinery"] = 0.05

    assert portfolio_activation_fingerprint(shadow) == (portfolio_activation_fingerprint(active))

    active["optimizer"]["sector_weight_caps"]["defense"] = 0.10
    assert portfolio_activation_fingerprint(shadow) != (portfolio_activation_fingerprint(active))


def test_activation_date_must_not_precede_candidate_or_start() -> None:
    lock = {
        "promotion_candidate_asof": "2026-07-24",
        "production_start_date": "2026-07-24",
    }

    with pytest.raises(ValueError, match="predate"):
        _activation_date_checks(lock, "2026-07-23")
    _activation_date_checks(lock, "2026-07-24")
    _activation_date_checks(lock, "2026-07-27")


def test_activation_wall_clock_refuses_future_date() -> None:
    with pytest.raises(ValueError, match="in the future"):
        validate_wall_clock("2026-07-27", today=date(2026, 7, 25))

    validate_wall_clock("2026-07-27", today=date(2026, 7, 27))


def test_activation_requires_completed_same_day_session() -> None:
    with pytest.raises(ValueError, match="not a completed session"):
        validate_completed_session(
            "2026-07-27",
            now_et=datetime.fromisoformat("2026-07-27T16:59:00-04:00"),
        )

    validate_completed_session(
        "2026-07-27",
        now_et=datetime.fromisoformat("2026-07-27T17:00:00-04:00"),
    )
    validate_completed_session(
        "2026-07-24",
        now_et=datetime.fromisoformat("2026-07-27T09:00:00-04:00"),
    )


def test_portfolio_activation_renderer_changes_only_reviewed_fields() -> None:
    path = Path("portfolio_layer/config.yaml")
    active_bytes = path.read_bytes()
    shadow_bytes = render_portfolio_activation_config(
        active_bytes,
        required=False,
        cap=0.0,
    )
    before = yaml.safe_load(shadow_bytes)
    updated = render_portfolio_activation_config(
        shadow_bytes,
        required=True,
        cap=0.05,
    )
    after = yaml.safe_load(updated)

    before_family = next(row for row in before["score_contract"]["sectors"] if row["model_family"] == "machinery")
    after_family = next(row for row in after["score_contract"]["sectors"] if row["model_family"] == "machinery")
    assert before_family["required"] is False
    assert after_family["required"] is True
    assert before["optimizer"]["sector_weight_caps"]["machinery"] == 0.0
    assert after["optimizer"]["sector_weight_caps"]["machinery"] == 0.05
    assert portfolio_activation_fingerprint(before) == (portfolio_activation_fingerprint(after))


def test_survivorship_sidecar_demotes_production_allocation_fields() -> None:
    rows = survivorship_sidecar(
        [
            {
                "ticker": "CAT",
                "rank_ready_flag": "1",
                "portfolio_universe_eligible_flag": "1",
                "portfolio_selection_policy": "long_only_q20_equal",
                "portfolio_sleeve_selected_flag": "1",
                "portfolio_sleeve_target_weight": "0.05",
                "portfolio_candidate_gate": "1",
                "portfolio_candidate_score": "72.5",
                "portfolio_candidate_status": "eligible",
                "portfolio_candidate_reason": "ok",
                "oos_score_valid_flag": "1",
                "oos_score_asof_date": "2026-07-27",
                "oos_invalid_reason": "",
            }
        ]
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["portfolio_universe_eligible_flag"] == ""
    assert row["portfolio_selection_policy"] == ""
    assert row["portfolio_sleeve_selected_flag"] == ""
    assert row["portfolio_sleeve_target_weight"] == ""
    assert row["portfolio_candidate_gate"] == "0"
    assert row["portfolio_candidate_score"] == "72.5"
    assert row["portfolio_candidate_status"] == "shadow_only"
    assert row["oos_score_valid_flag"] == "0"
    assert row["oos_score_asof_date"] == ""
    assert row["oos_invalid_reason"] == "shadow_pre_oos_calibration"
    assert row["stage11_calibration_input_eligible_flag"] == "1"
    assert row["calibration_sample_role"] == "pre_lock_research"


def test_production_dashboard_retains_shadow_calibration_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path("industrials/machinery/scripts/10b_validate_machinery_dashboard_reports.py")
    validate = namespace["validate_dashboard_artifacts"]
    globals_map = validate.__globals__
    production_fields = {
        "portfolio_universe_eligible_flag",
        "portfolio_selection_policy",
        "portfolio_sleeve_selected_flag",
        "portfolio_sleeve_target_weight",
    }
    full_fields = [
        "asof_date",
        "ticker",
        "final_rank",
        "rank_ready_flag",
        "portfolio_candidate_gate",
        "stage11_calibration_input_eligible_flag",
        "calibration_sample_role",
        "survivorship_corrected_panel_flag",
        "scoring_contract_version",
        *sorted(production_fields),
    ]
    monkeypatch.setitem(globals_map, "FINAL_RANK_FIELDS", full_fields)
    monkeypatch.setitem(
        globals_map,
        "PRODUCTION_SELECTION_FIELDS",
        production_fields,
    )
    monkeypatch.setitem(
        globals_map,
        "validate_rank_rows",
        lambda *_args, **_kwargs: [],
    )

    rank_rows = []
    sidecar_rows = []
    for ticker, production_rank, shadow_rank in (
        ("AAA", "1", "2"),
        ("BBB", "2", "1"),
    ):
        common = {
            "asof_date": "2026-07-24",
            "ticker": ticker,
            "rank_ready_flag": "1",
            "stage11_calibration_input_eligible_flag": "1",
            "calibration_sample_role": "pre_lock_research",
            "survivorship_corrected_panel_flag": "1",
        }
        sidecar_rows.append(
            {
                **common,
                "final_rank": shadow_rank,
                "portfolio_candidate_gate": "0",
                "scoring_contract_version": "shadow",
            }
        )
        rank_rows.append(
            {
                **common,
                "final_rank": production_rank,
                "portfolio_candidate_gate": ("1" if ticker == "AAA" else "0"),
                "scoring_contract_version": "production",
                "portfolio_universe_eligible_flag": "1",
                "portfolio_selection_policy": "q20",
                "portfolio_sleeve_selected_flag": ("1" if ticker == "AAA" else "0"),
                "portfolio_sleeve_target_weight": ("1" if ticker == "AAA" else "0"),
            }
        )
    rank_path = tmp_path / "machinery_final_rank_table.csv"
    sidecar_path = tmp_path / "machinery_stage11_survivorship_calibration_panel.csv"
    _write_csv(rank_path, rank_rows)
    _write_csv(sidecar_path, sidecar_rows)
    manifest = {
        "acceptance": "PASS",
        "model_family": "machinery",
        "asof_date": "2026-07-24",
        "row_count": 2,
        "rank_ready_count": 2,
        "portfolio_candidate_count": 1,
        "selected_sleeve_count": 1,
        "sidecar_calibration_eligible_count": 2,
        "production_policy_active": True,
        "activation_metadata": {"activation_asof": "2026-07-24"},
        "sidecar_retained_shadow": True,
        "contract_fields": full_fields,
        "scoring_contract_versions": ["production"],
        "rank_table_sha256": file_sha256(rank_path),
        "sidecar_sha256": file_sha256(sidecar_path),
    }
    (tmp_path / "machinery_final_rank_table_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    errors, count = validate(tmp_path, asof="2026-07-24")

    assert errors == []
    assert count == 2


def test_activation_lock_is_owned_and_released(tmp_path: Path) -> None:
    lock_path = tmp_path / ".orchestrator.lock"
    with ActivationOrchestrationLock(lock_path):
        text = lock_path.read_text(encoding="utf-8")
        assert f"pid={os.getpid()}" in text
    assert not lock_path.exists()


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_portfolio_smoke_requires_exact_membership_and_equal_weights(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    db = tmp_path / "db" / "portfolio.sqlite"
    config_path = tmp_path / "portfolio.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "paths": {
                    "database_path": str(db),
                    "output_dir": str(output),
                    "cache_dir": str(tmp_path / "cache"),
                    "macro_serving_db_path": str(tmp_path / "macro.sqlite"),
                },
                "score_contract": {
                    "sectors": [
                        {
                            "model_family": "machinery",
                            "required": True,
                        }
                    ]
                },
                "optimizer": {
                    "sector_weight_caps": {"machinery": 0.05},
                    "fixed_equal_weight_sleeves": ["machinery"],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    asof = "2026-07-27"
    run_dir = output / "runs" / asof
    run_dir.mkdir(parents=True)
    (run_dir / "orchestration_meta.json").write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "groups_completed": sorted(REQUIRED_PORTFOLIO_GROUPS),
            }
        ),
        encoding="utf-8",
    )
    _write_csv(
        run_dir / "stocks_scores.csv",
        [
            {
                "ticker": ticker,
                "source_pipeline": "machinery",
                "investable_eligible": 1,
            }
            for ticker in ("AAA", "BBB")
        ],
    )
    _write_csv(
        run_dir / "optimizer" / "target_weights.csv",
        [
            {
                "ticker": ticker,
                "source_pipeline": "machinery",
                "weight": 0.025,
            }
            for ticker in ("AAA", "BBB")
        ],
    )
    final_manifest = run_dir / "final" / "final_manifest.json"
    final_manifest.parent.mkdir(parents=True)
    final_manifest.write_text(
        json.dumps({"acceptance": "PASS"}),
        encoding="utf-8",
    )
    activation = ActivationPaths(tmp_path / "stage12", asof)
    _write_csv(
        activation.rank_csv,
        [
            {
                "ticker": ticker,
                "portfolio_sleeve_selected_flag": 1,
            }
            for ticker in ("AAA", "BBB")
        ],
    )

    result = validate_portfolio_smoke(
        portfolio_config_path=config_path,
        asof=asof,
        activation_paths=activation,
    )

    assert result["acceptance"] == "PASS"
    assert result["stage1_machinery_investable_count"] == 2
    assert result["optimizer_machinery_weight"] == pytest.approx(0.05)

    manifest_records = []
    for group in sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS):
        manifest_path = run_dir / "resume" / f"{group}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps({"acceptance": "PASS"}),
            encoding="utf-8",
        )
        manifest_records.append(
            {
                "group": group,
                "manifest": str(manifest_path),
                "manifest_sha256": file_sha256(manifest_path),
            }
        )
    resume_evidence = tmp_path / "portfolio_prefix_resume_evidence.json"
    resume_evidence.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "active_config_sha256": file_sha256(config_path),
                "groups": sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS),
                "manifests": manifest_records,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "orchestration_meta.json").write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "groups_completed": sorted(PORTFOLIO_RESUME_GROUPS),
            }
        ),
        encoding="utf-8",
    )
    resumed = validate_portfolio_smoke(
        portfolio_config_path=config_path,
        asof=asof,
        activation_paths=activation,
        reused_groups=REUSABLE_PORTFOLIO_PREFIX_GROUPS,
        resume_evidence_path=resume_evidence,
    )
    assert resumed["acceptance"] == "PASS"
    assert resumed["reused_groups"] == sorted(REUSABLE_PORTFOLIO_PREFIX_GROUPS)

    _write_csv(
        run_dir / "optimizer" / "target_weights.csv",
        [
            {
                "ticker": "AAA",
                "source_pipeline": "machinery",
                "weight": 0.05,
            }
        ],
    )
    _write_csv(
        run_dir / "optimizer" / "monitor_eligibility_overlay.csv",
        [
            {
                "ticker": "BBB",
                "source_pipeline": "machinery",
                "optimizer_entry_eligible": 0,
            }
        ],
    )
    filtered = validate_portfolio_smoke(
        portfolio_config_path=config_path,
        asof=asof,
        activation_paths=activation,
        reused_groups=REUSABLE_PORTFOLIO_PREFIX_GROUPS,
        resume_evidence_path=resume_evidence,
    )
    assert filtered["optimizer_machinery_monitor_excluded_count"] == 1

    _write_csv(
        run_dir / "optimizer" / "monitor_eligibility_overlay.csv",
        [
            {
                "ticker": "BBB",
                "source_pipeline": "machinery",
                "optimizer_entry_eligible": 1,
            }
        ],
    )
    with pytest.raises(ValueError, match="without a monitor entry exclusion"):
        validate_portfolio_smoke(
            portfolio_config_path=config_path,
            asof=asof,
            activation_paths=activation,
            reused_groups=REUSABLE_PORTFOLIO_PREFIX_GROUPS,
            resume_evidence_path=resume_evidence,
        )
    weight_path = run_dir / "optimizer" / "target_weights.csv"
    _write_csv(
        weight_path,
        [
            {
                "ticker": "AAA",
                "source_pipeline": "machinery",
                "weight": 0.03,
            },
            {
                "ticker": "BBB",
                "source_pipeline": "machinery",
                "weight": 0.02,
            },
        ],
    )
    with pytest.raises(ValueError, match="equal weighting"):
        validate_portfolio_smoke(
            portfolio_config_path=config_path,
            asof=asof,
            activation_paths=activation,
            reused_groups=REUSABLE_PORTFOLIO_PREFIX_GROUPS,
            resume_evidence_path=resume_evidence,
        )


def test_resume_contract_reruns_monitor_and_downstream_groups() -> None:
    reusable = {
        "scores",
        "risk",
    }
    assert REUSABLE_PORTFOLIO_PREFIX_GROUPS == reusable
    assert {
        "ledger",
        "monitor",
        "monitor_filter",
        "macro_contract",
        "bl",
        "sleeves",
        "exits",
        "payout",
        "governor",
        "final",
        "final_report",
    } <= REQUIRED_PORTFOLIO_GROUPS

    resume = list(PORTFOLIO_RESUME_GROUPS)
    assert reusable.isdisjoint(resume)
    assert resume.index("ledger") < resume.index("monitor")
    assert resume.index("monitor") < resume.index("monitor_filter")
    assert resume.index("monitor_filter") < resume.index("bl")
    assert resume.index("bl") < resume.index("sleeves")
    assert resume.index("sleeves") < resume.index("exits")
    assert resume.index("final") < resume.index("final_report")
    satisfied = set(resume)
    for group, aliases in PORTFOLIO_COMPLETION_ALIASES.items():
        if group in satisfied:
            satisfied.update(aliases)
    assert REQUIRED_PORTFOLIO_GROUPS - reusable <= satisfied


def test_published_candidate_rollback_restores_exact_shadow(
    tmp_path: Path,
) -> None:
    asof = "2026-07-27"
    governance_root = tmp_path / "stage12"
    paths = ActivationPaths(governance_root, asof)
    paths.root.mkdir(parents=True)
    live_rank = tmp_path / "dashboard" / asof / "machinery_final_rank_table.csv"
    live_manifest = tmp_path / "dashboard" / asof / "machinery_final_rank_table_manifest.json"
    live_sidecar = live_rank.with_name("machinery_stage11_survivorship_calibration_panel.csv")
    _write_csv(
        live_rank,
        [
            {
                "ticker": "AAA",
                "asof_date": asof,
                "portfolio_candidate_gate": 0,
                "oos_score_valid_flag": 0,
            }
        ],
    )
    shadow_rank = live_rank.read_bytes()
    live_sidecar.write_text("shadow-sidecar\n", encoding="utf-8")
    shadow_sidecar = live_sidecar.read_bytes()
    shadow_manifest_payload = {
        "acceptance": "PASS",
        "asof_date": asof,
        "rank_table_sha256": file_sha256(live_rank),
    }
    live_manifest.write_text(
        json.dumps(shadow_manifest_payload),
        encoding="utf-8",
    )
    shadow_manifest = live_manifest.read_bytes()
    paths.shadow_backup_csv.write_bytes(shadow_rank)
    paths.shadow_sidecar_backup_csv.write_bytes(shadow_sidecar)
    paths.shadow_manifest_backup_json.write_bytes(shadow_manifest)
    paths.manifest_json.write_text(
        json.dumps(
            {
                "source_shadow_rank": str(live_rank),
                "publish_sidecar": str(live_sidecar),
                "source_shadow_manifest": str(live_manifest),
            }
        ),
        encoding="utf-8",
    )
    live_rank.write_text("promoted\n", encoding="utf-8")
    live_sidecar.write_text("promoted-sidecar\n", encoding="utf-8")
    live_manifest.write_text("{}", encoding="utf-8")

    result = rollback_published_candidate(
        governance_root=governance_root,
        asof=asof,
        reason="smoke failed",
    )

    assert result["acceptance"] == "PASS"
    assert live_rank.read_bytes() == shadow_rank
    assert live_sidecar.read_bytes() == shadow_sidecar
    assert live_manifest.read_bytes() == shadow_manifest
    assert result["sidecar_sha256"] == file_sha256(live_sidecar)


def test_dashboard_snapshot_restores_missing_and_partial_targets(
    tmp_path: Path,
) -> None:
    paths = ActivationPaths(tmp_path / "stage12", "2026-08-03")
    live_root = tmp_path / "dashboard" / "2026-08-03"
    rank = live_root / "machinery_final_rank_table.csv"
    sidecar = live_root / "machinery_stage11_survivorship_calibration_panel.csv"
    manifest = live_root / "machinery_final_rank_table_manifest.json"
    rank.parent.mkdir(parents=True)
    rank.write_bytes(b"partial-rank\n")
    original_rank = rank.read_bytes()

    snapshot = _capture_dashboard_state(
        paths,
        live_rank=rank,
        live_sidecar=sidecar,
        live_manifest=manifest,
    )
    assert snapshot["complete_dashboard"] is False
    assert snapshot["files"]["rank"]["existed"] is True
    assert snapshot["files"]["sidecar"]["existed"] is False

    rank.write_bytes(b"published-rank\n")
    sidecar.write_bytes(b"published-sidecar\n")
    manifest.write_text("{}", encoding="utf-8")
    restored = _restore_dashboard_state(
        paths,
        live_rank=rank,
        live_sidecar=sidecar,
        live_manifest=manifest,
    )

    assert restored == snapshot
    assert rank.read_bytes() == original_rank
    assert not sidecar.exists()
    assert not manifest.exists()


def test_activation_transaction_rolls_back_config_and_dashboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.machinery.stage12_activation_transaction as transaction

    asof = "2026-07-25"
    governance_root = tmp_path / "stage12"
    stage12_paths = transaction.Stage12Paths(governance_root)
    stage12_paths.root.mkdir(parents=True)
    portfolio_config_path = tmp_path / "portfolio.yaml"
    portfolio_payload = {
        "paths": {
            "database_path": str(tmp_path / "portfolio.sqlite"),
            "output_dir": str(tmp_path / "portfolio_output"),
            "cache_dir": str(tmp_path / "cache"),
            "macro_serving_db_path": str(tmp_path / "macro.sqlite"),
        },
        "score_contract": {
            "sector_output_root": str(tmp_path / "sector_output"),
            "sectors": [
                {
                    "model_family": "machinery",
                    "required": False,
                    "file_mode": "dated",
                    "file_path": ("industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv"),
                }
            ],
        },
        "optimizer": {
            "sector_weight_caps": {"machinery": 0.0},
            "fixed_equal_weight_sleeves": ["machinery"],
        },
    }
    portfolio_config_path.write_text(
        yaml.safe_dump(portfolio_payload, sort_keys=False),
        encoding="utf-8",
    )
    original_config = portfolio_config_path.read_bytes()
    lock_payload = {
        "promotion_candidate_asof": "2026-07-24",
        "production_start_date": asof,
        "proposed_portfolio_cap": 0.05,
        "portfolio_non_activation_config_sha256": (portfolio_activation_fingerprint(portfolio_payload)),
        "machinery_portfolio_policy_sha256": (machinery_portfolio_policy_fingerprint(portfolio_payload)),
    }

    stage12_paths.lock_json.write_text(
        json.dumps(lock_payload),
        encoding="utf-8",
    )
    machinery_config_path = tmp_path / "machinery.yaml"
    machinery_config = {
        "machinery_stage12": {
            "portfolio_config_path": str(portfolio_config_path),
            "activation_approval_token": "APPROVE",
        }
    }
    machinery_config_path.write_text(
        yaml.safe_dump(machinery_config),
        encoding="utf-8",
    )
    activation_paths = ActivationPaths(governance_root, asof)
    live_rank = (
        tmp_path / "sector_output" / "industrials" / "machinery" / "dashboard" / asof / "machinery_final_rank_table.csv"
    )
    live_manifest = live_rank.with_name("machinery_final_rank_table_manifest.json")
    _write_csv(
        live_rank,
        [
            {
                "ticker": "AAA",
                "asof_date": asof,
                "portfolio_candidate_gate": 0,
                "oos_score_valid_flag": 0,
            }
        ],
    )
    shadow_rank = live_rank.read_bytes()
    live_manifest.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "rank_table_sha256": file_sha256(live_rank),
            }
        ),
        encoding="utf-8",
    )
    shadow_manifest = live_manifest.read_bytes()

    def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        activation_paths.root.mkdir(parents=True, exist_ok=True)
        _write_csv(
            activation_paths.rank_csv,
            [
                {
                    "ticker": "AAA",
                    "portfolio_sleeve_selected_flag": 1,
                }
            ],
        )
        activation_paths.manifest_json.write_text(
            json.dumps(
                {
                    "source_shadow_rank": str(live_rank),
                    "source_shadow_manifest": str(live_manifest),
                }
            ),
            encoding="utf-8",
        )
        return {"acceptance": "PASS"}

    def fake_activate(*_args: object, **_kwargs: object) -> dict[str, object]:
        active = yaml.safe_load(portfolio_config_path.read_text(encoding="utf-8"))
        assert active["score_contract"]["sectors"][0]["required"] is True
        assert active["optimizer"]["sector_weight_caps"]["machinery"] == 0.05
        activation_paths.shadow_backup_csv.write_bytes(shadow_rank)
        activation_paths.shadow_manifest_backup_json.write_bytes(shadow_manifest)
        live_rank.write_text("promoted\n", encoding="utf-8")
        live_manifest.write_text("{}", encoding="utf-8")
        return {"acceptance": "PASS"}

    def fake_command(
        command: list[str],
        *,
        log_path: Path,
        lock: ActivationOrchestrationLock,
    ) -> CommandResult:
        del lock
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("simulated portfolio failure", encoding="utf-8")
        return CommandResult(
            command=command,
            return_code=1,
            log_path=log_path,
        )

    monkeypatch.setattr(
        transaction,
        "validate_stage12_lock",
        lambda **_kwargs: {"acceptance": "PASS", "issues": []},
    )
    monkeypatch.setattr(transaction, "prepare_activation_candidate", fake_prepare)
    monkeypatch.setattr(transaction, "activate_candidate", fake_activate)
    monkeypatch.setattr(transaction, "run_logged_command", fake_command)
    monkeypatch.setattr(
        transaction,
        "ActivationOrchestrationLock",
        lambda: ActivationOrchestrationLock(tmp_path / "orchestrator.lock"),
    )

    result = run_activation_transaction(
        machinery_config,
        config_path=machinery_config_path,
        governance_root=governance_root,
        asof=asof,
        approval_token="APPROVE",
        run_refresh=False,
        force_candidate=False,
        reuse_risk_price_data=False,
    )

    assert result["acceptance"] == "FAIL_ROLLED_BACK"
    assert result["portfolio_config_restored"] is True
    assert portfolio_config_path.read_bytes() == original_config
    assert live_rank.read_bytes() == shadow_rank
    assert live_manifest.read_bytes() == shadow_manifest
    assert not (tmp_path / "orchestrator.lock").exists()


def test_stage12_runtime_uses_configured_python_and_probes_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.machinery.stage12_activation_transaction as transaction

    configured_python = tmp_path / "portfolio-python.exe"
    configured_python.write_bytes(b"test")
    probes: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> object:
        probes.append(command)
        return type("Probe", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    monkeypatch.setattr(transaction.subprocess, "run", fake_run)
    resolved = resolve_stage12_python(
        {"machinery_stage12": {"python_executable": str(configured_python)}},
        config_path=tmp_path / "config.yaml",
    )

    assert resolved == configured_python
    assert probes == [
        [
            str(configured_python),
            "-c",
            "import exchange_calendars, numpy, pandas, sklearn, yaml",
        ]
    ]


def test_stage12_runtime_fails_before_activation_when_dependencies_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.machinery.stage12_activation_transaction as transaction

    configured_python = tmp_path / "portfolio-python.exe"
    configured_python.write_bytes(b"test")

    def fake_run(_command: list[str], **_kwargs: object) -> object:
        return type(
            "Probe",
            (),
            {"returncode": 1, "stderr": "No module named sklearn", "stdout": ""},
        )()

    monkeypatch.setattr(transaction.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="missing portfolio dependencies"):
        resolve_stage12_python(
            {"machinery_stage12": {"python_executable": str(configured_python)}},
            config_path=tmp_path / "config.yaml",
        )


def test_active_model_replacement_failure_preserves_prior_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.machinery.stage12_activation_transaction as transaction

    asof = "2026-07-28"
    cycle_root = tmp_path / "stage12" / "cycles" / "v2"
    cycle_paths = transaction.Stage12Paths(cycle_root)
    cycle_paths.root.mkdir(parents=True)
    active_root = tmp_path / "stage12"
    active_paths = transaction.Stage12Paths(active_root)
    active_paths.root.mkdir(parents=True, exist_ok=True)
    prior_state = b'{"acceptance":"PASS","production_policy_status":"ACTIVE"}\n'
    active_paths.activation_state_json.write_bytes(prior_state)

    portfolio_config_path = tmp_path / "portfolio.yaml"
    portfolio_payload = {
        "paths": {
            "database_path": str(tmp_path / "portfolio.sqlite"),
            "output_dir": str(tmp_path / "portfolio_output"),
            "cache_dir": str(tmp_path / "cache"),
            "macro_serving_db_path": str(tmp_path / "macro.sqlite"),
        },
        "score_contract": {
            "sector_output_root": str(tmp_path / "sector_output"),
            "sectors": [
                {
                    "model_family": "machinery",
                    "required": True,
                    "file_mode": "dated",
                    "file_path": ("industrials/machinery/dashboard/{yyyy-mm-dd}/machinery_final_rank_table.csv"),
                }
            ],
        },
        "optimizer": {
            "sector_weight_caps": {"machinery": 0.05},
            "fixed_equal_weight_sleeves": ["machinery"],
        },
    }
    portfolio_config_path.write_text(
        yaml.safe_dump(portfolio_payload, sort_keys=False),
        encoding="utf-8",
    )
    original_config = portfolio_config_path.read_bytes()
    lock_payload = {
        "activation_mode": ACTIVATION_MODE_REPLACE_ACTIVE,
        "promotion_candidate_asof": asof,
        "production_start_date": "2026-07-24",
        "proposed_portfolio_cap": 0.05,
        "active_activation_state": str(active_paths.activation_state_json),
        "previous_activation_state_sha256": file_sha256(active_paths.activation_state_json),
        "portfolio_non_activation_config_sha256": (portfolio_activation_fingerprint(portfolio_payload)),
        "machinery_portfolio_policy_sha256": (machinery_portfolio_policy_fingerprint(portfolio_payload)),
    }
    cycle_paths.lock_json.write_text(
        json.dumps(lock_payload),
        encoding="utf-8",
    )
    machinery_config_path = tmp_path / "machinery.yaml"
    machinery_config = {
        "machinery_stage12": {
            "output_root": str(active_root),
            "portfolio_config_path": str(portfolio_config_path),
            "activation_approval_token": "APPROVE",
        }
    }
    machinery_config_path.write_text(
        yaml.safe_dump(machinery_config),
        encoding="utf-8",
    )
    activation_paths = ActivationPaths(cycle_root, asof)
    live_rank = (
        tmp_path / "sector_output" / "industrials" / "machinery" / "dashboard" / asof / "machinery_final_rank_table.csv"
    )
    live_manifest = live_rank.with_name("machinery_final_rank_table_manifest.json")
    _write_csv(live_rank, [{"ticker": "OLD", "asof_date": asof}])
    original_rank = live_rank.read_bytes()
    live_manifest.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "asof_date": asof,
                "rank_table_sha256": file_sha256(live_rank),
            }
        ),
        encoding="utf-8",
    )
    original_manifest = live_manifest.read_bytes()

    def fake_prepare(*_args: object, **_kwargs: object) -> dict[str, object]:
        activation_paths.root.mkdir(parents=True, exist_ok=True)
        _write_csv(
            activation_paths.rank_csv,
            [{"ticker": "NEW", "portfolio_sleeve_selected_flag": 1}],
        )
        activation_paths.manifest_json.write_text(
            json.dumps(
                {
                    "publish_rank": str(live_rank),
                    "publish_manifest": str(live_manifest),
                }
            ),
            encoding="utf-8",
        )
        return {"acceptance": "PASS"}

    def fake_activate(*_args: object, **_kwargs: object) -> dict[str, object]:
        assert portfolio_config_path.read_bytes() == original_config
        activation_paths.shadow_backup_csv.write_bytes(original_rank)
        activation_paths.shadow_manifest_backup_json.write_bytes(original_manifest)
        live_rank.write_text("promoted\n", encoding="utf-8")
        live_manifest.write_text("{}", encoding="utf-8")
        return {"acceptance": "PASS"}

    def fake_command(
        command: list[str],
        *,
        log_path: Path,
        lock: ActivationOrchestrationLock,
    ) -> CommandResult:
        del lock
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("simulated portfolio failure", encoding="utf-8")
        return CommandResult(command=command, return_code=1, log_path=log_path)

    monkeypatch.setattr(
        transaction,
        "validate_stage12_lock",
        lambda **_kwargs: {"acceptance": "PASS", "issues": []},
    )
    monkeypatch.setattr(transaction, "prepare_activation_candidate", fake_prepare)
    monkeypatch.setattr(transaction, "activate_candidate", fake_activate)
    monkeypatch.setattr(transaction, "run_logged_command", fake_command)
    monkeypatch.setattr(
        transaction,
        "ActivationOrchestrationLock",
        lambda: ActivationOrchestrationLock(tmp_path / "orchestrator.lock"),
    )

    result = run_activation_transaction(
        machinery_config,
        config_path=machinery_config_path,
        governance_root=cycle_root,
        asof=asof,
        approval_token="APPROVE",
        run_refresh=False,
        force_candidate=False,
        reuse_risk_price_data=False,
    )

    assert result["acceptance"] == "FAIL_ROLLED_BACK"
    assert portfolio_config_path.read_bytes() == original_config
    assert active_paths.activation_state_json.read_bytes() == prior_state
    assert live_rank.read_bytes() == original_rank
    assert live_manifest.read_bytes() == original_manifest


def test_daily_scoring_requires_untampered_active_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import industrials.machinery.stage12_activation as activation

    activation_asof = "2026-07-25"
    governance_root = tmp_path / "stage12"
    stage12_paths = activation.Stage12Paths(governance_root)
    stage12_paths.root.mkdir(parents=True)
    lock_payload = {
        "promotion_candidate_asof": "2026-07-24",
        "production_start_date": activation_asof,
        "proposed_portfolio_cap": 0.05,
        "production_selection_policy": {
            "variant": "long_only_q20_equal",
            "minimum_positions": 1,
            "universe_policy": "operating_only",
        },
        "recommended_weights": {"quality_score": 1.0},
        "lockbox_start_date": "2026-01-01",
    }
    stage12_paths.lock_json.write_text(
        json.dumps(lock_payload),
        encoding="utf-8",
    )
    candidate_paths = ActivationPaths(governance_root, activation_asof)
    candidate_paths.root.mkdir(parents=True)
    candidate_paths.rank_csv.write_text("candidate\n", encoding="utf-8")
    candidate_paths.activation_json.write_text(
        json.dumps(
            {
                "acceptance": "PASS",
                "activation_status": ACTIVATION_STATUS_FULLY_VALIDATED,
                "asof_date": activation_asof,
                "full_portfolio_smoke_required": False,
            }
        ),
        encoding="utf-8",
    )
    portfolio_config_path = tmp_path / "portfolio.yaml"
    portfolio_config_path.write_text(
        yaml.safe_dump(
            {
                "score_contract": {
                    "sectors": [
                        {
                            "model_family": "machinery",
                            "required": True,
                        }
                    ]
                },
                "optimizer": {
                    "sector_weight_caps": {"machinery": 0.05},
                    "fixed_equal_weight_sleeves": ["machinery"],
                },
            }
        ),
        encoding="utf-8",
    )
    lock_payload["machinery_portfolio_policy_sha256"] = machinery_portfolio_policy_fingerprint(
        yaml.safe_load(portfolio_config_path.read_text(encoding="utf-8"))
    )
    stage12_paths.lock_json.write_text(
        json.dumps(lock_payload),
        encoding="utf-8",
    )
    machinery_config_path = tmp_path / "machinery.yaml"
    machinery_config = {
        "machinery_stage12": {
            "portfolio_config_path": str(portfolio_config_path),
            "score_model_version": "score-v1",
            "model_version": "model-v1",
            "scoring_contract_version": "contract-v1",
        }
    }
    machinery_config_path.write_text(
        yaml.safe_dump(machinery_config),
        encoding="utf-8",
    )
    state = {
        "acceptance": "PASS",
        "production_policy_status": "ACTIVE",
        "activation_asof": activation_asof,
        "governance_lock_sha256": file_sha256(stage12_paths.lock_json),
        "candidate_rank": str(candidate_paths.rank_csv),
        "candidate_rank_sha256": file_sha256(candidate_paths.rank_csv),
        "activation_result": str(candidate_paths.activation_json),
        "activation_result_sha256": file_sha256(candidate_paths.activation_json),
        "production_source_sha256": production_policy_source_hashes(),
    }
    stage12_paths.activation_state_json.write_text(
        json.dumps(state),
        encoding="utf-8",
    )
    production_rows = [
        {
            "ticker": "AAA",
            "portfolio_sleeve_selected_flag": "1",
            "portfolio_universe_eligible_flag": "1",
        }
    ]
    monkeypatch.setattr(
        activation,
        "_sealed_governance",
        lambda *_args, **_kwargs: (lock_payload, stage12_paths),
    )
    monkeypatch.setattr(
        activation,
        "strategy_spec_by_name",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        activation,
        "production_preview_rows",
        lambda *_args, **_kwargs: production_rows,
    )
    monkeypatch.setattr(
        activation,
        "_validate_preview_rows",
        lambda *_args, **_kwargs: [],
    )

    rows, metadata = apply_active_production_policy(
        machinery_config,
        config_path=machinery_config_path,
        governance_root=governance_root,
        asof="2026-07-26",
        shadow_rows=[{"ticker": "AAA"}],
    )

    assert rows == production_rows
    assert metadata["production_policy_active"] is True
    assert metadata["selected_sleeve_count"] == 1

    candidate_paths.activation_json.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="activation result changed"):
        apply_active_production_policy(
            machinery_config,
            config_path=machinery_config_path,
            governance_root=governance_root,
            asof="2026-07-26",
            shadow_rows=[{"ticker": "AAA"}],
        )
