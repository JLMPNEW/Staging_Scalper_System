from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from portfolio_layer.scores.adapters import (
    CONSUMER_DEFENSIVE_REQUIRED_COLUMNS,
    CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS,
    validate_consumer_v3_optimizer_cap_binding,
    run_adapter,
)


COHORTS = (
    "beverages",
    "consumer_staples_distribution_retail",
    "household_personal_tobacco",
    "packaged_foods_agricultural_products",
)
MODEL_VERSION = "consumer_defensive_v3_portfolio_test"
DECISION_SHA = "a" * 64
FRAMEWORK_SHA = "b" * 64
INPUT_SHA = "c" * 64
SCORE_MANIFEST_FILENAME = "consumer_defensive_production_score_manifest_v3.json"
TERMINAL_MANIFEST_TEMPLATE = (
    "orchestration/{yyyy-mm-dd}/"
    "consumer_defensive_production_refresh_manifest_v3.json"
)


def _canonical_sha256(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scope_contract() -> dict[str, Any]:
    scope: dict[str, Any] = {
        "mode": "explicit_ticker_exclusions",
        "enforcement_stage": "before_cross_section_normalization",
        "selection_basis": "unit_test_governed_scope",
        "evidence_classification": "unit_test",
        "strict_oos_eligible": True,
        "preserve_source_history": True,
        "production_promotion_requires_fresh_post_scope_evidence": True,
        "reviewed_as_of": "2026-08-25",
        "excluded_tickers_by_cohort": {cohort: [] for cohort in COHORTS},
        "excluded_tickers": [],
        "excluded_ticker_count": 0,
        "expected_remaining_current_ticker_count": 1,
        "expected_remaining_current_tickers_sha256": _value_sha256(["KO"]),
        "expected_remaining_current_by_cohort": {
            cohort: int(cohort == "beverages") for cohort in COHORTS
        },
    }
    scope["payload_sha256"] = _canonical_sha256(scope)
    return scope


def _lock_id(
    *, cohort: str, effective_from: str, valid_until: str, model_contract_sha256: str
) -> str:
    identity = {
        "cohort": cohort,
        "decision_sha256": DECISION_SHA,
        "effective_from": effective_from,
        "model_contract_sha256": model_contract_sha256,
        "valid_until": valid_until,
    }
    encoded = json.dumps(
        identity,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return "cdv3_" + hashlib.sha256(encoded).hexdigest()[:24]


def _registry(
    *,
    asof_date: str = "2026-08-19",
    effective_from: str = "2026-08-20",
    valid_until: str = "2026-10-21",
    failed_cohorts: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    locks: dict[str, dict[str, Any]] = {}
    for position, cohort in enumerate(COHORTS, start=1):
        promoted = cohort not in failed_cohorts
        model_contract_sha = str(position) * 64
        lock: dict[str, Any] = {
            "schema_version": "consumer_defensive_activation_lock_v3",
            "model_family": "consumer_defensive",
            "cohort": cohort,
            "lock_id": _lock_id(
                cohort=cohort,
                effective_from=effective_from,
                valid_until=valid_until,
                model_contract_sha256=model_contract_sha,
            ),
            "effective_from": effective_from,
            "valid_until": valid_until,
            "deployment_state": "active_full" if promoted else "benchmark_production",
            "promotion_state": "promoted" if promoted else "shadow_monitor",
            "investable": promoted,
            "tier_deployment_fraction": 1.0 if promoted else 0.0,
            "effective_deployment_fraction": 1.0 if promoted else 0.0,
            "approved_full_portfolio_cap": 0.03125,
            "optimizer_cap": 0.03125 if promoted else 0.0,
            "expected_alpha_at_full": 0.04 if promoted else 0.0,
            "confidence_multiplier": 0.80,
            "decision_sha256": DECISION_SHA,
            "framework_sha256": FRAMEWORK_SHA,
            "source_input_sha256": INPUT_SHA,
            "model_contract_sha256": model_contract_sha,
            "selected_candidate_id": f"{cohort}_candidate_v1",
            "score_model_version": MODEL_VERSION,
            "scoring_contract_version": MODEL_VERSION,
        }
        lock["payload_sha256"] = _canonical_sha256(lock)
        locks[cohort] = lock

    registry: dict[str, Any] = {
        "schema_version": "consumer_defensive_production_activation_registry_v3",
        "model_family": "consumer_defensive",
        "asof_date": asof_date,
        "effective_from": effective_from,
        "valid_until": valid_until,
        "maximum_activation_age_days": 63,
        "decision_sha256": DECISION_SHA,
        "framework_sha256": FRAMEWORK_SHA,
        "source_input_sha256": INPUT_SHA,
        "calibration_write_performed": False,
        "portfolio_write_performed": False,
        "cohorts": locks,
    }
    registry["payload_sha256"] = _canonical_sha256(registry)
    return registry


def _config(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "model_family": "consumer_defensive",
        "adapter": "consumer_defensive",
        "sector": "Consumer Staples",
        "industry": "Consumer Defensive",
        "industry_aggregate": "Consumer Staples",
        "file_mode": "flat",
        "file_path": "rank.csv",
        "require_oos_score_valid": True,
        "calibration": {
            "neutral": "median",
            "scale": 50.0,
            "expected_alpha_at_full": 0.0,
        },
        "production_activation_registry_file_path": "",
        "production_activation_registry_sha256": "",
        "production_change_control_public_key_path": "",
        "optimizer_sector_cap": 0.0,
    }
    if registry is not None:
        cfg["production_activation_registry_file_path"] = "activation_registry.json"
        cfg["production_activation_registry_sha256"] = registry["payload_sha256"]
        cfg["calibration_by_scope"] = {
            cohort: {
                "neutral": "median",
                "scale": 50.0,
                "expected_alpha_at_full": registry["cohorts"][cohort][
                    "expected_alpha_at_full"
                ]
                if registry["cohorts"][cohort]["promotion_state"] == "promoted"
                else 0.0,
            }
            for cohort in COHORTS
        }
        cfg["optimizer_cap_by_scope"] = {
            cohort: registry["cohorts"][cohort]["optimizer_cap"]
            if registry["cohorts"][cohort]["promotion_state"] == "promoted"
            else 0.0
            for cohort in COHORTS
        }
        cfg["optimizer_sector_cap"] = sum(
            registry["cohorts"][cohort]["approved_full_portfolio_cap"]
            for cohort in COHORTS
        )
    return cfg


def _row(registry: dict[str, Any] | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {field: "" for field in CONSUMER_DEFENSIVE_REQUIRED_COLUMNS}
    promoted = registry is not None
    row.update(
        {
            "asof_date": "2026-08-25",
            "ticker": "KO",
            "company_name": "The Coca-Cola Company",
            "sector": "Consumer Staples",
            "industry": "Consumer Defensive",
            "industry_aggregate": "Consumer Staples",
            "calibration_cohort": "beverages",
            "final_score": "72.5",
            "final_rank": "1",
            "rank_ready_flag": "1",
            "model_status": "complete",
            "score_confidence": "0.9",
            "score_model_version": MODEL_VERSION,
            "model_version": MODEL_VERSION,
            "scoring_contract_version": MODEL_VERSION,
            "portfolio_candidate_gate": "1" if promoted else "0",
            "portfolio_candidate_score": "72.5",
            "portfolio_candidate_status": "eligible" if promoted else "not_eligible",
            "portfolio_candidate_reason": "ok" if promoted else "governance_shadow",
            "calibration_eligible_flag": "1",
            "research_calibration_input_eligible_flag": "0",
            "research_calibration_reason": "not_survivorship_corrected",
            "calibration_sample_role": "strict_oos" if promoted else "excluded",
            "stage11_calibration_panel_source": "dated_rank_table",
            "stage11_calibration_input_eligible_flag": "0",
            "stage11_calibration_input_reason": "not_survivorship_corrected",
            "survivorship_corrected_panel_flag": "0",
            "oos_score_valid_flag": "1" if promoted else "0",
            "oos_score_asof_date": "2026-08-25" if promoted else "",
            "oos_invalid_reason": "" if promoted else "governance_shadow",
            "calibration_lock_date": "2026-08-24" if promoted else "",
            "promotion_state": "promoted" if promoted else "shadow_monitor",
        }
    )
    if registry is not None:
        lock = registry["cohorts"]["beverages"]
        row.update(
            {
                "consumer_defensive_production_lock_id": lock["lock_id"],
                "consumer_defensive_production_lock_sha256": lock["payload_sha256"],
                "consumer_defensive_model_contract_sha256": lock[
                    "model_contract_sha256"
                ],
                "consumer_defensive_decision_sha256": lock["decision_sha256"],
                "consumer_defensive_selected_candidate_id": lock[
                    "selected_candidate_id"
                ],
                "consumer_defensive_deployment_state": lock["deployment_state"],
                "consumer_defensive_optimizer_cap": lock["optimizer_cap"],
                "consumer_defensive_confidence_multiplier": lock[
                    "confidence_multiplier"
                ],
            }
        )
    return row


def _write_csv(path: Path, row: dict[str, Any]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)


def _write_registry(path: Path, registry: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(registry, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _rehash_registry(registry: dict[str, Any]) -> dict[str, Any]:
    changed = copy.deepcopy(registry)
    for lock in changed["cohorts"].values():
        lock["payload_sha256"] = _canonical_sha256(lock)
    changed["payload_sha256"] = _canonical_sha256(changed)
    return changed


def _run(
    tmp_path: Path,
    cfg: dict[str, Any],
    row: dict[str, Any],
    *,
    run_as_of: str | None = None,
):
    effective_cfg = dict(cfg)
    effective_row = dict(row)
    governed = bool(
        str(cfg.get("production_activation_registry_file_path") or "").strip()
        or str(cfg.get("production_activation_registry_sha256") or "").strip()
        or str(row.get("promotion_state") or "").strip().lower() == "promoted"
    )
    rank_path = tmp_path / "rank.csv"
    if governed:
        scope = _scope_contract()
        scope_sha = str(scope["payload_sha256"])
        effective_cfg["production_score_manifest_filename"] = SCORE_MANIFEST_FILENAME
        effective_cfg["production_calibration_scope_sha256"] = scope_sha
        effective_cfg[
            "production_terminal_manifest_file_path"
        ] = TERMINAL_MANIFEST_TEMPLATE
        effective_row["consumer_defensive_calibration_scope_sha256"] = scope_sha
        effective_row = {
            field: effective_row.get(field, "")
            for field in CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS
        }
        row_body = {
            field: "" if effective_row[field] is None else str(effective_row[field])
            for field in CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS
            if field != "row_sha256"
        }
        effective_row["row_sha256"] = _canonical_sha256(row_body)
    _write_csv(rank_path, effective_row)
    if governed:
        cohort_counts = {
            cohort: int(
                str(effective_row.get("calibration_cohort") or "").strip()
                == cohort
            )
            for cohort in COHORTS
        }
        manifest: dict[str, Any] = {
            "schema_version": "consumer_defensive_production_score_manifest_v3",
            "status": "PASS",
            "allocation_asof_date": str(effective_row["asof_date"]),
            "signal_asof_date": str(
                effective_row.get("signal_asof_date") or ""
            ),
            "rank_csv_path": rank_path.resolve().as_posix(),
            "rank_csv_file_sha256": hashlib.sha256(rank_path.read_bytes()).hexdigest(),
            "rank_row_count": 1,
            "published_ticker_count": 1,
            "published_tickers_sha256": _value_sha256(
                [str(effective_row["ticker"]).strip().upper()]
            ),
            "published_tickers_by_cohort": cohort_counts,
            "calibration_scope_contract": scope,
            "calibration_scope_sha256": scope["payload_sha256"],
        }
        manifest["payload_sha256"] = _canonical_sha256(manifest)
        score_manifest_path = tmp_path / SCORE_MANIFEST_FILENAME
        score_manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        terminal_path = tmp_path / TERMINAL_MANIFEST_TEMPLATE.replace(
            "{yyyy-mm-dd}", str(effective_row["asof_date"])
        )
        terminal_path.parent.mkdir(parents=True, exist_ok=True)
        terminal = {
            "schema_version": (
                "consumer_defensive_production_refresh_manifest_v3"
            ),
            "status": "PASS",
            "asof_date": str(effective_row["asof_date"]),
            "signal_asof_date": str(
                effective_row.get("signal_asof_date") or ""
            ),
            "failure": None,
            "steps": [
                {"sequence": 1, "status": "PASS", "return_code": 0}
            ],
            "artifacts": {
                "rank_table": {
                    "path": str(rank_path.resolve()),
                    "sha256": hashlib.sha256(
                        rank_path.read_bytes()
                    ).hexdigest(),
                    "rows": 1,
                    "rank_ready_rows": 1,
                    "oos_valid_rows": int(
                        str(effective_row.get("oos_score_valid_flag") or "0")
                    ),
                    "calibration_scope_sha256": scope_sha,
                },
                "publisher_manifest": {
                    "path": str(score_manifest_path.resolve()),
                    "sha256": hashlib.sha256(
                        score_manifest_path.read_bytes()
                    ).hexdigest(),
                    "status": "PASS",
                },
            },
        }
        terminal_path.write_text(
            json.dumps(terminal, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    return run_adapter(effective_cfg, tmp_path, run_as_of)


def test_valid_pinned_registry_authorizes_row_and_is_archived_as_source(
    tmp_path: Path,
) -> None:
    registry = _registry()
    registry_path = tmp_path / "activation_registry.json"
    _write_registry(registry_path, registry)

    result = _run(tmp_path, _config(registry), _row(registry))

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 1
    assert result.rows[0].production_policy_id == registry["cohorts"]["beverages"][
        "lock_id"
    ]
    assert result.rows[0].production_policy_sha256 == registry["cohorts"][
        "beverages"
    ]["payload_sha256"]
    assert result.source_files == (
        (tmp_path / "rank.csv").resolve(),
        (tmp_path / SCORE_MANIFEST_FILENAME).resolve(),
        (
            tmp_path
            / TERMINAL_MANIFEST_TEMPLATE.replace(
                "{yyyy-mm-dd}", "2026-08-25"
            )
        ).resolve(),
        registry_path.resolve(),
    )


def test_all_four_eligible_cohorts_receive_standard_equal_authority() -> None:
    registry = _registry()

    assert all(
        lock["deployment_state"] == "active_full"
        and lock["optimizer_cap"] == 0.03125
        and lock["effective_deployment_fraction"] == 1.0
        for lock in registry["cohorts"].values()
    )
    assert sum(
        lock["optimizer_cap"] for lock in registry["cohorts"].values()
    ) == 0.125


def test_nonpromoted_cohort_slot_remains_cash_without_lowering_sector_ceiling(
    tmp_path: Path,
) -> None:
    registry = _registry(failed_cohorts=frozenset({"household_personal_tobacco"}))
    _write_registry(tmp_path / "activation_registry.json", registry)
    cfg = _config(registry)
    optimizer = _optimizer_config_for(cfg)

    result = _run(tmp_path, cfg, _row(registry))
    validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)

    assert result.rows[0].investable_eligible == 1
    assert cfg["optimizer_sector_cap"] == 0.125
    assert sum(cfg["optimizer_cap_by_scope"].values()) == 0.09375
    assert cfg["optimizer_cap_by_scope"]["household_personal_tobacco"] == 0.0


def test_retired_canary_registry_cannot_be_reactivated(tmp_path: Path) -> None:
    registry = _registry()
    lock = registry["cohorts"]["beverages"]
    lock["deployment_state"] = "experimental_canary"
    lock["tier_deployment_fraction"] = 0.10
    lock["effective_deployment_fraction"] = 0.10
    lock["optimizer_cap"] = 0.003125
    registry = _rehash_registry(registry)
    _write_registry(tmp_path / "activation_registry.json", registry)

    with pytest.raises(ValueError, match="invalid state"):
        _run(tmp_path, _config(registry), _row(registry))


def test_registry_requires_equal_reserved_cohort_slots(tmp_path: Path) -> None:
    registry = _registry()
    lock = registry["cohorts"]["beverages"]
    lock["approved_full_portfolio_cap"] = 0.04
    lock["optimizer_cap"] = 0.04
    registry = _rehash_registry(registry)
    _write_registry(tmp_path / "activation_registry.json", registry)

    with pytest.raises(ValueError, match="equal cohort allocation slots"):
        _run(tmp_path, _config(registry), _row(registry))


def test_configured_sector_cap_must_match_registry_reserved_slots(
    tmp_path: Path,
) -> None:
    registry = _registry()
    _write_registry(tmp_path / "activation_registry.json", registry)
    cfg = _config(registry)
    cfg["optimizer_sector_cap"] = 0.09375

    with pytest.raises(ValueError, match="registry allocation slots"):
        _run(tmp_path, cfg, _row(registry))


@pytest.mark.parametrize("configured_field", ["path", "sha"])
def test_registry_path_and_sha_must_be_configured_together(
    tmp_path: Path, configured_field: str
) -> None:
    registry = _registry()
    _write_registry(tmp_path / "activation_registry.json", registry)
    cfg = _config()
    if configured_field == "path":
        cfg["production_activation_registry_file_path"] = "activation_registry.json"
    else:
        cfg["production_activation_registry_sha256"] = registry["payload_sha256"]

    with pytest.raises(ValueError, match="configured together"):
        _run(tmp_path, cfg, _row())


def test_validly_rehashed_registry_still_fails_an_old_external_pin(tmp_path: Path) -> None:
    original = _registry()
    cfg = _config(original)
    changed = copy.deepcopy(original)
    changed["asof_date"] = "2026-08-20"
    changed["payload_sha256"] = _canonical_sha256(changed)
    _write_registry(tmp_path / "activation_registry.json", changed)

    with pytest.raises(ValueError, match="configured SHA pin"):
        _run(tmp_path, cfg, _row(original))


@pytest.mark.parametrize(
    ("registry", "message"),
    [
        (
            _registry(
                asof_date="2026-08-25",
                effective_from="2026-08-26",
                valid_until="2026-10-27",
            ),
            "outside registry authority dates",
        ),
        (
            _registry(
                asof_date="2026-06-22",
                effective_from="2026-06-22",
                valid_until="2026-08-24",
            ),
            "outside registry authority dates",
        ),
    ],
    ids=["not_yet_effective", "expired"],
)
def test_registry_authority_window_is_enforced(
    tmp_path: Path, registry: dict[str, Any], message: str
) -> None:
    _write_registry(tmp_path / "activation_registry.json", registry)

    with pytest.raises(ValueError, match=message):
        _run(tmp_path, _config(registry), _row(registry))


def test_validly_rehashed_registry_cannot_rewindow_a_stale_decision(
    tmp_path: Path,
) -> None:
    registry = _registry(
        asof_date="2026-01-01",
        effective_from="2026-03-06",
        valid_until="2026-03-06",
    )
    _write_registry(tmp_path / "activation_registry.json", registry)

    with pytest.raises(ValueError, match="decision-anchored authority window"):
        _run(tmp_path, _config(registry), _row(registry))


def test_portfolio_run_date_cannot_use_registry_after_expiry(tmp_path: Path) -> None:
    registry = _registry()
    _write_registry(tmp_path / "activation_registry.json", registry)

    with pytest.raises(
        ValueError,
        match="Portfolio run date falls outside Consumer registry authority dates",
    ):
        _run(
            tmp_path,
            _config(registry),
            _row(registry),
            run_as_of="2026-10-22",
        )


@pytest.mark.parametrize(
    ("field", "bad_value", "message"),
    [
        (
            "consumer_defensive_selected_candidate_id",
            "unselected_candidate",
            "row/lock mismatch",
        ),
        ("score_model_version", "wrong_model", "wrong score model version"),
        (
            "consumer_defensive_production_lock_id",
            "cdv3_wrong_lock",
            "row/lock mismatch",
        ),
        ("consumer_defensive_optimizer_cap", "0.02", "wrong optimizer cap"),
    ],
    ids=["candidate", "model", "lock", "cap"],
)
def test_rank_row_must_match_registry_authority_exactly(
    tmp_path: Path, field: str, bad_value: str, message: str
) -> None:
    registry = _registry()
    _write_registry(tmp_path / "activation_registry.json", registry)
    row = _row(registry)
    row[field] = bad_value

    with pytest.raises(ValueError, match=message):
        _run(tmp_path, _config(registry), row)


def test_registry_binds_config_for_cohorts_without_rank_rows(tmp_path: Path) -> None:
    registry = _registry()
    _write_registry(tmp_path / "activation_registry.json", registry)
    cfg = _config(registry)
    cfg["optimizer_cap_by_scope"]["household_personal_tobacco"] = 0.01

    with pytest.raises(
        ValueError,
        match="configured cap household_personal_tobacco is not bound",
    ):
        _run(tmp_path, cfg, _row(registry))



def test_unconfigured_shadow_snapshot_remains_readable_and_noninvestable(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, _config(), _row())

    assert len(result.rows) == 1
    assert result.rows[0].investable_eligible == 0
    assert result.rows[0].oos_score_valid_flag == 0
    assert result.source_files == ((tmp_path / "rank.csv").resolve(),)


def _optimizer_config_for(score_cfg: dict) -> dict:
    caps = dict(score_cfg["optimizer_cap_by_scope"])
    return {
        "gross_exposure": 1.0,
        "scope_weight_caps": {"consumer_defensive": caps},
        "sector_weight_caps": {
            "consumer_defensive": score_cfg["optimizer_sector_cap"]
        },
    }


def test_optimizer_cap_surfaces_are_bound_to_reviewed_scope_caps() -> None:
    cfg = _config()
    cfg["optimizer_cap_by_scope"] = {
        cohort: 0.005 for cohort in COHORTS
    }
    cfg["optimizer_sector_cap"] = 0.02
    optimizer = _optimizer_config_for(cfg)
    validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)

    optimizer["scope_weight_caps"]["consumer_defensive"]["beverages"] = 0.05
    with pytest.raises(ValueError, match="scope caps diverge"):
        validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)


def test_sector_cap_must_match_score_contract_and_cover_cohort_authority() -> None:
    cfg = _config()
    cfg["optimizer_cap_by_scope"] = {
        cohort: 0.005 for cohort in COHORTS
    }
    cfg["optimizer_sector_cap"] = 0.02
    optimizer = _optimizer_config_for(cfg)
    optimizer["sector_weight_caps"]["consumer_defensive"] = 0.50
    with pytest.raises(ValueError, match="sector caps diverge"):
        validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)

    cfg["optimizer_sector_cap"] = 0.01
    optimizer["sector_weight_caps"]["consumer_defensive"] = 0.01
    with pytest.raises(ValueError, match="cohort authority exceeds"):
        validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)

    cfg["optimizer_sector_cap"] = 0.0
    optimizer["sector_weight_caps"]["consumer_defensive"] = 0.0
    with pytest.raises(ValueError, match="requires a positive sector cap"):
        validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)


def test_positive_consumer_authority_rejects_leveraged_gross() -> None:
    cfg = _config()
    cfg["optimizer_cap_by_scope"] = {
        cohort: 0.005 for cohort in COHORTS
    }
    cfg["optimizer_sector_cap"] = 0.02
    optimizer = _optimizer_config_for(cfg)
    optimizer["gross_exposure"] = 1.01

    with pytest.raises(ValueError, match="gross exposure in"):
        validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)


def test_zero_shadow_cap_surfaces_remain_valid() -> None:
    cfg = _config()
    cfg["optimizer_cap_by_scope"] = {
        cohort: 0.0 for cohort in COHORTS
    }
    optimizer = _optimizer_config_for(cfg)
    validate_consumer_v3_optimizer_cap_binding(cfg, optimizer)
