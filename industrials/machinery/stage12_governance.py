from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.core.reports import write_csv_atomic
from industrials.machinery.scoring import (
    FINAL_RANK_FIELDS,
    file_sha256,
    read_rows,
    write_json_atomic,
    write_rank_rows,
)
from industrials.machinery.stage8_calibration import (
    COMPONENT_FIELDS,
    as_float,
    parse_date,
    stage8_paths,
    utc_now,
    validate_stage8,
)
from industrials.machinery.stage9_backtest import (
    PRODUCTION_SELECTION_POLICY_VERSION,
    StrategySpec,
    portfolio_weights,
    production_universe_eligible,
    production_universe_policy,
    stage9_paths,
    strategy_spec_by_name,
    validate_stage9,
)
from portfolio_layer.scores.adapters import run_adapter


MODEL_FAMILY = "machinery"
CONFIG_KEY = "machinery_stage12"
ACTIVATION_MODE_INITIAL = "initial_activation"
ACTIVATION_MODE_REPLACE_ACTIVE = "replace_active_model"
SLEEVE_TARGET_FIELDS = (
    "asof_date",
    "ticker",
    "final_rank",
    "final_score",
    "portfolio_universe_eligible_flag",
    "portfolio_sleeve_selected_flag",
    "portfolio_sleeve_target_weight",
    "portfolio_selection_policy",
)


class Stage12Paths:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.preview_csv = root / "machinery_production_rank_table_preview.csv"
        self.sleeve_targets_csv = (
            root / "machinery_production_sleeve_targets.csv"
        )
        self.lock_json = root / "machinery_stage12_governance_lock.json"
        self.manifest_json = root / "machinery_stage12_manifest.json"
        self.validation_json = root / "machinery_stage12_validation.json"
        self.activation_state_json = (
            root / "machinery_production_activation_state.json"
        )


def _portfolio_family(
    portfolio_config: dict[str, Any],
) -> dict[str, Any]:
    families = cfg_get(portfolio_config, "score_contract.sectors", [])
    if not isinstance(families, list):
        raise ValueError("portfolio_layer sector score families must be a list")
    matches = [
        item
        for item in families
        if isinstance(item, dict)
        and str(item.get("model_family") or "") == MODEL_FAMILY
    ]
    if len(matches) != 1:
        raise ValueError(
            "portfolio_layer must configure exactly one machinery family"
        )
    return dict(matches[0])


def portfolio_activation_fingerprint(
    portfolio_config: Mapping[str, Any],
) -> str:
    normalized = copy.deepcopy(dict(portfolio_config))
    families = cfg_get(normalized, "score_contract.sectors", [])
    if not isinstance(families, list):
        raise ValueError("portfolio_layer sector score families must be a list")
    for family in families:
        if (
            isinstance(family, dict)
            and str(family.get("model_family") or "") == MODEL_FAMILY
        ):
            family["required"] = "<machinery-activation-setting>"
    sector_caps = cfg_get(normalized, "optimizer.sector_weight_caps", {})
    if not isinstance(sector_caps, dict):
        raise ValueError("portfolio_layer sector weight caps must be a mapping")
    if MODEL_FAMILY not in sector_caps:
        raise ValueError("portfolio_layer machinery allocation cap is missing")
    sector_caps[MODEL_FAMILY] = "<machinery-activation-setting>"
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def machinery_portfolio_policy_fingerprint(
    portfolio_config: Mapping[str, Any],
) -> str:
    """Hash machinery policy while ignoring only activation toggles."""
    config = dict(portfolio_config)
    family = _portfolio_family(config)
    family["required"] = "<machinery-activation-setting>"
    payload = {
        "score_contract_family": family,
        "optimizer": {
            "sector_weight_cap": "<machinery-activation-setting>",
            "fixed_equal_weight": MODEL_FAMILY
            in {
                str(value)
                for value in cfg_get(
                    config,
                    "optimizer.fixed_equal_weight_sleeves",
                    [],
                )
            },
        },
        "risk_panel_etf": cfg_get(
            config,
            f"risk_panel.sector_etf_map.{MODEL_FAMILY}",
        ),
        "sleeve_factor_etf": cfg_get(
            config,
            f"sleeves.sector_factor_etfs.{MODEL_FAMILY}",
        ),
        "black_litterman_strategic_weight": cfg_get(
            config,
            f"black_litterman_fusion.strategic_sector_weights.{MODEL_FAMILY}",
        ),
        "macro_family_mapping": cfg_get(
            config,
            f"macro.family_mappings.{MODEL_FAMILY}",
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _score(
    row: Mapping[str, str],
    weights: Mapping[str, float],
) -> float:
    total = 0.0
    for field in COMPONENT_FIELDS:
        value = as_float(row.get(field))
        if value is None:
            raise ValueError(
                f"{row.get('ticker')}: missing calibrated component {field}"
            )
        total += value * float(weights[field])
    return max(0.0, min(100.0, total))


def production_preview_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    weights: Mapping[str, float],
    asof: str,
    lock_date: str,
    score_model_version: str,
    model_version: str,
    scoring_contract_version: str,
    selection_spec: StrategySpec,
    minimum_positions: int,
    universe_policy: str,
) -> list[dict[str, str]]:
    if selection_spec.portfolio_type != "long_only":
        raise ValueError("Stage 12 supports only a validated long-only policy")
    scored: list[tuple[dict[str, str], float]] = []
    for source in rows:
        row = {
            field: str(source.get(field) or "") for field in FINAL_RANK_FIELDS
        }
        score = _score(row, weights)
        row["final_score"] = f"{score:.8f}".rstrip("0").rstrip(".")
        row["portfolio_candidate_score"] = row["final_score"]
        scored.append((row, score))
    universe = [
        (row, score)
        for row, score in scored
        if (
            str(row.get("rank_ready_flag") or "") == "1"
            and str(row.get("model_status") or "") == "complete"
            and str(row.get("rank_ready_reason") or "") == "ok"
            and production_universe_eligible(
                row,
                policy=universe_policy,
            )
        )
    ]
    sleeve_weights = portfolio_weights(
        universe,
        spec=selection_spec,
        minimum_positions=minimum_positions,
    )
    if not sleeve_weights or any(
        weight <= 0 for weight in sleeve_weights.values()
    ):
        raise ValueError("Stage 12 production sleeve selection is empty or non-long")
    scored.sort(
        key=lambda item: (
            -int(str(item[0].get("rank_ready_flag") or "0")),
            -item[1],
            str(item[0].get("ticker") or ""),
        )
    )
    output: list[dict[str, str]] = []
    for rank, (row, _) in enumerate(scored, start=1):
        rank_eligible = (
            str(row.get("rank_ready_flag") or "") == "1"
            and str(row.get("model_status") or "") == "complete"
            and str(row.get("rank_ready_reason") or "") == "ok"
        )
        universe_eligible = rank_eligible and production_universe_eligible(
            row,
            policy=universe_policy,
        )
        ticker = str(row.get("ticker") or "")
        selected = ticker in sleeve_weights
        rank_reason = str(
            row.get("rank_ready_reason") or "not_rank_ready"
        )
        candidate_reason = (
            "ok"
            if selected
            else (
                "outside_validated_top_quantile"
                if universe_eligible
                else "development_stage_core_sleeve_excluded"
                if rank_eligible
                else rank_reason
            )
        )
        row.update(
            {
                "final_rank": str(rank),
                "score_model_version": score_model_version,
                "model_version": model_version,
                "scoring_contract_version": scoring_contract_version,
                "portfolio_universe_eligible_flag": (
                    "1" if universe_eligible else "0"
                ),
                "portfolio_selection_policy": selection_spec.name,
                "portfolio_sleeve_selected_flag": (
                    "1" if selected else "0"
                ),
                "portfolio_sleeve_target_weight": (
                    f"{sleeve_weights[ticker]:.12f}".rstrip("0").rstrip(".")
                    if selected
                    else "0"
                ),
                "portfolio_candidate_gate": "1" if selected else "0",
                "portfolio_candidate_status": (
                    "eligible"
                    if selected
                    else (
                        "not_selected"
                        if universe_eligible
                        else "not_eligible"
                    )
                ),
                "portfolio_candidate_reason": candidate_reason,
                "calibration_eligible_flag": (
                    "1" if rank_eligible else "0"
                ),
                "research_calibration_input_eligible_flag": (
                    "1" if rank_eligible else "0"
                ),
                "research_calibration_reason": (
                    "ok" if rank_eligible else rank_reason
                ),
                "calibration_sample_role": (
                    "strict_oos" if rank_eligible else "excluded"
                ),
                "stage11_calibration_panel_source": (
                    "dashboard_rank_snapshot_current_universe_replay"
                ),
                "stage11_calibration_input_eligible_flag": (
                    "1" if rank_eligible else "0"
                ),
                "stage11_calibration_input_reason": (
                    "ok" if rank_eligible else rank_reason
                ),
                "survivorship_corrected_panel_flag": "0",
                "oos_score_valid_flag": (
                    "1" if rank_eligible else "0"
                ),
                "oos_score_asof_date": (
                    asof if rank_eligible else ""
                ),
                "oos_invalid_reason": (
                    "" if rank_eligible else rank_reason
                ),
                "calibration_lock_date": lock_date,
            }
        )
        output.append({field: row.get(field, "") for field in FINAL_RANK_FIELDS})
    return output


def _validate_preview_rows(
    rows: Sequence[Mapping[str, str]],
    *,
    asof: str,
    selection_policy: Mapping[str, Any],
) -> list[str]:
    issues: list[str] = []
    if not rows:
        return ["production preview is empty"]
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        issues.append("production preview as-of mismatch")
    ranks = [int(str(row.get("final_rank") or "0")) for row in rows]
    if sorted(ranks) != list(range(1, len(rows) + 1)):
        issues.append("production preview ranks are not contiguous")
    expected_policy = str(selection_policy.get("variant") or "")
    selected_weights: list[float] = []
    selected_tickers: set[str] = set()
    universe_eligible_count = 0
    for row in rows:
        ticker = str(row.get("ticker") or "<blank>")
        universe_eligible = (
            str(row.get("portfolio_universe_eligible_flag") or "") == "1"
        )
        universe_eligible_count += int(universe_eligible)
        selected = (
            str(row.get("portfolio_sleeve_selected_flag") or "") == "1"
        )
        candidate = str(row.get("portfolio_candidate_gate") or "") == "1"
        if row.get("portfolio_selection_policy") != expected_policy:
            issues.append(f"{ticker}: production selection policy mismatch")
            break
        try:
            target_weight = float(
                str(row.get("portfolio_sleeve_target_weight") or "0")
            )
        except ValueError:
            issues.append(f"{ticker}: invalid sleeve target weight")
            break
        if selected:
            selected_tickers.add(ticker)
            selected_weights.append(target_weight)
        if selected and (
            not universe_eligible
            or not candidate
            or row.get("portfolio_candidate_status") != "eligible"
            or row.get("portfolio_candidate_reason") != "ok"
            or row.get("oos_score_valid_flag") != "1"
            or row.get("calibration_sample_role") != "strict_oos"
            or target_weight <= 0
        ):
            issues.append(f"{ticker}: invalid proposed production flags")
            break
        if not selected and (candidate or target_weight != 0):
            issues.append(f"{ticker}: unselected row has portfolio allocation")
            break
        if universe_eligible and not selected and (
            row.get("portfolio_candidate_status") != "not_selected"
            or row.get("portfolio_candidate_reason")
            != "outside_validated_top_quantile"
            or row.get("oos_score_valid_flag") != "1"
            or row.get("calibration_sample_role") != "strict_oos"
        ):
            issues.append(f"{ticker}: broad eligible row lost OOS validity")
            break
        if not universe_eligible:
            core_excluded = (
                row.get("portfolio_candidate_reason")
                == "development_stage_core_sleeve_excluded"
            )
            if core_excluded:
                if (
                    row.get("oos_score_valid_flag") != "1"
                    or row.get("calibration_sample_role") != "strict_oos"
                    or row.get("research_calibration_input_eligible_flag")
                    != "1"
                ):
                    issues.append(
                        f"{ticker}: core-excluded research row lost OOS validity"
                    )
                    break
            elif row.get("oos_score_valid_flag") != "0":
                issues.append(f"{ticker}: ineligible row proposed as OOS valid")
                break
    if not selected_weights:
        issues.append("production preview selected sleeve is empty")
    elif abs(sum(selected_weights) - 1.0) > 1e-8:
        issues.append("production sleeve target weights do not sum to 1")
    if (
        selected_weights
        and str(selection_policy.get("weighting") or "") == "equal"
        and max(selected_weights) - min(selected_weights) > 1e-10
    ):
        issues.append("production sleeve does not preserve equal weights")
    quantile = float(selection_policy.get("quantile") or 0.0)
    minimum_positions = int(selection_policy.get("minimum_positions") or 0)
    expected_count = min(
        universe_eligible_count,
        max(
            minimum_positions,
            math.ceil(universe_eligible_count * quantile),
        ),
    )
    if len(selected_tickers) != expected_count:
        issues.append(
            "production sleeve selected count does not match policy "
            f"expected={expected_count} actual={len(selected_tickers)}"
        )
    return issues


def build_stage12_lock(
    config: dict[str, Any],
    *,
    config_path: Path,
    stage8_root: Path,
    stage9_root: Path,
    output_root: Path,
    asof: str,
    allow_active_upgrade: bool = False,
    source_dashboard_dir: Path | None = None,
    active_governance_root: Path | None = None,
) -> dict[str, Any]:
    paths = Stage12Paths(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    stage8_validation = validate_stage8(
        config,
        output_root=stage8_root,
        require_stage9_ready=True,
    )
    stage9_validation = validate_stage9(
        config,
        stage8_root=stage8_root,
        output_root=stage9_root,
        require_stage12_ready=True,
    )
    issues = [
        *[
            f"Stage 8: {item}"
            for item in stage8_validation.get("issues", [])
        ],
        *[
            f"Stage 9: {item}"
            for item in stage9_validation.get("issues", [])
        ],
    ]
    if stage8_validation["acceptance"] != "PASS":
        issues.append("Stage 8 strict validation failed")
    if stage9_validation["acceptance"] != "PASS":
        issues.append("Stage 9 strict validation failed")
    if issues:
        raise ValueError(";".join(issues))
    portfolio_config_path = resolve_path(
        cfg_get(config, f"{CONFIG_KEY}.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio_config = load_yaml(portfolio_config_path)
    portfolio_family = _portfolio_family(portfolio_config)
    if portfolio_family.get("adapter") != "industrial_family":
        raise ValueError("machinery portfolio adapter is not industrial_family")
    if portfolio_family.get("require_oos_score_valid") is not True:
        raise ValueError("machinery portfolio adapter must require OOS validity")
    machinery_aum = float(
        cfg_get(config, "machinery_stage9.target_aum_usd", 0.0)
    )
    portfolio_aum = float(
        cfg_get(portfolio_config, "transaction_costs.aum_usd", 0.0)
    )
    if machinery_aum <= 0 or machinery_aum != portfolio_aum:
        raise ValueError(
            f"portfolio AUM mismatch machinery={machinery_aum} "
            f"portfolio_layer={portfolio_aum}"
        )
    stage8_acceptance_path = stage8_paths(stage8_root).acceptance_json
    stage9_acceptance_path = stage9_paths(stage9_root).acceptance_json
    stage8_acceptance = json.loads(
        stage8_acceptance_path.read_text(encoding="utf-8")
    )
    stage9_acceptance = json.loads(
        stage9_acceptance_path.read_text(encoding="utf-8")
    )
    selection_policy_raw = stage9_acceptance.get(
        "production_selection_policy"
    )
    if not isinstance(selection_policy_raw, Mapping):
        raise ValueError("Stage 9 production selection policy is missing")
    selection_policy = dict(selection_policy_raw)
    configured_universe_policy = production_universe_policy(config)
    if (
        selection_policy.get("version")
        != PRODUCTION_SELECTION_POLICY_VERSION
        or selection_policy.get("parity_status") != "PASS"
    ):
        raise ValueError("Stage 9 production selection policy is not sealed")
    recommended_variant = str(
        stage9_acceptance["recommended_variant_for_stage12"]
    )
    if selection_policy.get("variant") != recommended_variant:
        raise ValueError("Stage 9 recommended variant and policy disagree")
    if selection_policy.get("universe_policy") != configured_universe_policy:
        raise ValueError("Stage 9 production universe policy is inconsistent")
    selection_spec = strategy_spec_by_name(config, recommended_variant)
    minimum_positions = int(selection_policy.get("minimum_positions") or 0)
    if minimum_positions <= 0:
        raise ValueError("Stage 9 production minimum positions is invalid")
    if (
        selection_policy.get("portfolio_type")
        != selection_spec.portfolio_type
        or selection_policy.get("weighting") != selection_spec.weighting
        or float(selection_policy.get("quantile") or 0.0)
        != selection_spec.quantile
    ):
        raise ValueError("Stage 9 production selection policy is inconsistent")
    optimizer_config = cfg_get(portfolio_config, "optimizer", {})
    if not isinstance(optimizer_config, Mapping):
        raise ValueError("portfolio_layer optimizer configuration is invalid")
    fixed_equal_sleeves = {
        str(value)
        for value in optimizer_config.get("fixed_equal_weight_sleeves", [])
    }
    if (
        selection_spec.weighting == "equal"
        and MODEL_FAMILY not in fixed_equal_sleeves
    ):
        raise ValueError(
            "portfolio_layer must preserve equal weights for machinery"
        )
    sector_caps = optimizer_config.get("sector_weight_caps", {})
    if not isinstance(sector_caps, Mapping) or MODEL_FAMILY not in sector_caps:
        raise ValueError("portfolio_layer machinery allocation cap is missing")
    current_portfolio_cap = float(sector_caps[MODEL_FAMILY])
    proposed_portfolio_cap = float(
        cfg_get(config, f"{CONFIG_KEY}.proposed_portfolio_cap", 0.05)
    )
    if proposed_portfolio_cap <= 0 or proposed_portfolio_cap > 0.10:
        raise ValueError("proposed machinery portfolio cap must be in (0, 0.10]")
    active_state_path = Stage12Paths(
        active_governance_root
        or resolve_path(
            cfg_get(config, f"{CONFIG_KEY}.output_root"),
            base_dir=config_path.parent,
        )
    ).activation_state_json
    shadow_state = (
        portfolio_family.get("required") is False
        and current_portfolio_cap == 0.0
    )
    active_state = (
        portfolio_family.get("required") is True
        and current_portfolio_cap == proposed_portfolio_cap
    )
    previous_activation_state_sha256 = ""
    if shadow_state:
        activation_mode = ACTIVATION_MODE_INITIAL
        if active_state_path.exists():
            raise ValueError(
                "Initial Stage 12 candidate conflicts with an existing "
                "machinery activation state"
            )
    elif allow_active_upgrade and active_state:
        if not active_state_path.is_file():
            raise ValueError(
                "Active-model replacement requires the current machinery "
                "activation state"
            )
        previous_state = json.loads(
            active_state_path.read_text(encoding="utf-8")
        )
        if (
            previous_state.get("acceptance") != "PASS"
            or previous_state.get("production_policy_status") != "ACTIVE"
        ):
            raise ValueError(
                "Current machinery activation state is not active"
            )
        activation_mode = ACTIVATION_MODE_REPLACE_ACTIVE
        previous_activation_state_sha256 = file_sha256(active_state_path)
    else:
        raise ValueError(
            "Stage 12 requires either optional machinery with zero cap or "
            "an explicitly approved active-model replacement"
        )
    weights = {
        str(key): float(value)
        for key, value in stage9_acceptance["recommended_weights"].items()
    }
    if set(weights) != set(COMPONENT_FIELDS):
        raise ValueError("Stage 12 recommended weight fields are incomplete")
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=config_path.parent,
    )
    source_dir = source_dashboard_dir or (dashboard_root / asof)
    source_rank = source_dir / "machinery_final_rank_table.csv"
    source_manifest = source_dir / "machinery_final_rank_table_manifest.json"
    if not source_rank.exists() or not source_manifest.exists():
        raise FileNotFoundError(
            f"Stage 12 source dashboard is incomplete for {asof}"
        )
    dashboard_manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    if (
        dashboard_manifest.get("acceptance") != "PASS"
        or dashboard_manifest.get("rank_table_sha256")
        != file_sha256(source_rank)
    ):
        raise ValueError("Stage 12 source dashboard manifest is invalid")
    lock_date = str(stage8_acceptance["sealed_start_date"])
    if parse_date(asof) < parse_date(lock_date):
        raise ValueError("Stage 12 preview date predates the OOS lock")
    preview_rows = production_preview_rows(
        read_rows(source_rank),
        weights=weights,
        asof=asof,
        lock_date=lock_date,
        score_model_version=str(
            cfg_get(config, f"{CONFIG_KEY}.score_model_version")
        ),
        model_version=str(cfg_get(config, f"{CONFIG_KEY}.model_version")),
        scoring_contract_version=str(
            cfg_get(config, f"{CONFIG_KEY}.scoring_contract_version")
        ),
        selection_spec=selection_spec,
        minimum_positions=minimum_positions,
        universe_policy=configured_universe_policy,
    )
    selected_count = sum(
        row["portfolio_sleeve_selected_flag"] == "1"
        for row in preview_rows
    )
    selection_policy["governance_candidate_position_count"] = selected_count
    issues = _validate_preview_rows(
        preview_rows,
        asof=asof,
        selection_policy=selection_policy,
    )
    if issues:
        raise ValueError(";".join(issues))
    write_rank_rows(paths.preview_csv, preview_rows)
    sleeve_rows = [
        {field: row.get(field, "") for field in SLEEVE_TARGET_FIELDS}
        for row in preview_rows
        if row["portfolio_universe_eligible_flag"] == "1"
    ]
    write_csv_atomic(
        paths.sleeve_targets_csv,
        SLEEVE_TARGET_FIELDS,
        sleeve_rows,
    )
    adapter_config = dict(portfolio_family)
    adapter_config["file_mode"] = "flat"
    adapter_config["file_path"] = str(paths.preview_csv)
    adapter_result = run_adapter(
        adapter_config,
        config_path.parents[2],
        asof,
    )
    investable_count = sum(
        row.investable_eligible == 1 for row in adapter_result.rows
    )
    proposed_count = sum(
        row["portfolio_candidate_gate"] == "1" for row in preview_rows
    )
    if investable_count != proposed_count or proposed_count <= 0:
        raise ValueError(
            "Stage 12 portfolio adapter did not preserve proposed candidates"
        )
    selected_tickers = {
        row["ticker"]
        for row in preview_rows
        if row["portfolio_sleeve_selected_flag"] == "1"
    }
    adapter_tickers = {
        row.ticker
        for row in adapter_result.rows
        if row.investable_eligible == 1
    }
    if adapter_tickers != selected_tickers:
        raise ValueError(
            "Stage 12 portfolio adapter changed selected sleeve membership"
        )
    broad_eligible_count = sum(
        row["portfolio_universe_eligible_flag"] == "1"
        for row in preview_rows
    )
    lock = {
        "acceptance": "PASS",
        "stage12_status": "READY_NOT_ACTIVATED",
        "activation_mode": activation_mode,
        "model_family": MODEL_FAMILY,
        "created_at_utc": utc_now(),
        "promotion_candidate_asof": asof,
        "production_start_date": str(
            cfg_get(config, f"{CONFIG_KEY}.production_start_date")
        ),
        "development_end_date": stage8_acceptance["development_end_date"],
        "lockbox_start_date": stage8_acceptance["sealed_start_date"],
        "recommended_model": stage9_acceptance[
            "recommended_model_for_stage12"
        ],
        "recommended_variant": stage9_acceptance[
            "recommended_variant_for_stage12"
        ],
        "recommended_weights": weights,
        "production_selection_policy": selection_policy,
        "broad_portfolio_universe_eligible_count": broad_eligible_count,
        "selected_sleeve_count": selected_count,
        "selected_sleeve_target_weight_sum": sum(
            float(row["portfolio_sleeve_target_weight"])
            for row in preview_rows
            if row["portfolio_sleeve_selected_flag"] == "1"
        ),
        "target_aum_usd": machinery_aum,
        "current_portfolio_cap": current_portfolio_cap,
        "proposed_portfolio_cap": proposed_portfolio_cap,
        "portfolio_equal_weight_constraint_configured": (
            MODEL_FAMILY in fixed_equal_sleeves
        ),
        "minimum_capacity_multiple": stage9_acceptance[
            "minimum_capacity_multiple"
        ],
        "source_dashboard_asof": asof,
        "source_dashboard_rank": str(source_rank),
        "source_dashboard_manifest": str(source_manifest),
        "source_dashboard_sha256": file_sha256(source_rank),
        "source_dashboard_manifest_sha256": file_sha256(source_manifest),
        "stage8_run_manifest_sha256": file_sha256(
            stage8_paths(stage8_root).run_manifest_json
        ),
        "stage8_run_manifest": str(
            stage8_paths(stage8_root).run_manifest_json
        ),
        "stage9_run_manifest_sha256": file_sha256(
            stage9_paths(stage9_root).run_manifest_json
        ),
        "stage9_run_manifest": str(
            stage9_paths(stage9_root).run_manifest_json
        ),
        "portfolio_config_sha256": file_sha256(portfolio_config_path),
        "portfolio_non_activation_config_sha256": (
            portfolio_activation_fingerprint(portfolio_config)
        ),
        "machinery_portfolio_policy_sha256": (
            machinery_portfolio_policy_fingerprint(portfolio_config)
        ),
        "portfolio_adapter_sha256": file_sha256(
            config_path.parents[2] / "portfolio_layer" / "scores" / "adapters.py"
        ),
        "portfolio_optimizer_sha256": file_sha256(
            config_path.parents[2]
            / "portfolio_layer"
            / "optimizer"
            / "optimizer_core.py"
        ),
        "portfolio_optimizer_runner_sha256": file_sha256(
            config_path.parents[2]
            / "portfolio_layer"
            / "optimizer"
            / "09_run_portfolio_optimizer.py"
        ),
        "proposed_portfolio_candidate_count": proposed_count,
        "adapter_investable_count": investable_count,
        "production_promotion_performed": False,
        "live_dashboard_modified": False,
        "activation_requires_explicit_operator_approval": True,
        "active_activation_state": str(active_state_path),
        "previous_activation_state_sha256": (
            previous_activation_state_sha256
        ),
    }
    write_json_atomic(paths.lock_json, lock)
    artifacts = (
        paths.preview_csv,
        paths.sleeve_targets_csv,
        paths.lock_json,
    )
    manifest = {
        "artifact_family": "machinery_stage12_governance_candidate",
        "created_at_utc": utc_now(),
        "files": {
            path.name: {
                "path": str(path),
                "sha256": file_sha256(path),
            }
            for path in artifacts
        },
        "production_promotion_performed": False,
    }
    write_json_atomic(paths.manifest_json, manifest)
    return lock


def validate_stage12_lock(
    *,
    output_root: Path,
) -> dict[str, Any]:
    paths = Stage12Paths(output_root)
    issues: list[str] = []
    for path in (
        paths.preview_csv,
        paths.sleeve_targets_csv,
        paths.lock_json,
        paths.manifest_json,
    ):
        if not path.exists() or path.stat().st_size == 0:
            issues.append(f"missing Stage 12 artifact {path}")
    if issues:
        result = {"acceptance": "FAIL", "issues": issues}
        write_json_atomic(paths.validation_json, result)
        return result
    manifest = json.loads(paths.manifest_json.read_text(encoding="utf-8"))
    for metadata in manifest.get("files", {}).values():
        path = Path(str(metadata["path"]))
        if file_sha256(path) != str(metadata["sha256"]):
            issues.append(f"Stage 12 artifact hash mismatch {path}")
    lock = json.loads(paths.lock_json.read_text(encoding="utf-8"))
    rows = read_rows(paths.preview_csv)
    selection_policy = lock.get("production_selection_policy")
    if not isinstance(selection_policy, Mapping):
        issues.append("Stage 12 lock selection policy is missing")
        selection_policy = {}
    issues.extend(
        _validate_preview_rows(
            rows,
            asof=str(lock.get("promotion_candidate_asof") or ""),
            selection_policy=selection_policy,
        )
    )
    sleeve_rows = read_rows(paths.sleeve_targets_csv)
    broad_rows = [
        row
        for row in rows
        if row.get("portfolio_universe_eligible_flag") == "1"
    ]
    if sleeve_rows != [
        {field: row.get(field, "") for field in SLEEVE_TARGET_FIELDS}
        for row in broad_rows
    ]:
        issues.append("Stage 12 sleeve target artifact does not match preview")
    if lock.get("stage12_status") != "READY_NOT_ACTIVATED":
        issues.append("Stage 12 lock status is not READY_NOT_ACTIVATED")
    if lock.get("production_promotion_performed") is not False:
        issues.append("Stage 12 candidate unexpectedly performed promotion")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "stage12_status": lock.get("stage12_status"),
        "preview_rows": len(rows),
        "broad_portfolio_universe_eligible_count": lock.get(
            "broad_portfolio_universe_eligible_count"
        ),
        "selected_sleeve_count": lock.get("selected_sleeve_count"),
        "proposed_portfolio_candidate_count": lock.get(
            "proposed_portfolio_candidate_count"
        ),
        "adapter_investable_count": lock.get("adapter_investable_count"),
        "production_promotion_performed": False,
        "issues": issues,
    }
    write_json_atomic(paths.validation_json, result)
    return result
