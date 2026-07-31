"""Post-lockbox operational amendment for the fixed machinery v1.4 model."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.machinery.conditional_promotion_v14 import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_ROOT as CONDITIONAL_OUTPUT_ROOT,
    DEFAULT_PROTOCOL_PATH as CONDITIONAL_PROTOCOL_PATH,
    PROTOCOL_VERSION as CONDITIONAL_PROTOCOL_VERSION,
    _active_evidence,
    _float_map,
    conditional_paths,
)
from industrials.machinery.scoring import (
    file_sha256,
    read_rows,
    validate_rank_rows,
    write_json_atomic,
    write_rank_rows,
)
from industrials.machinery.stage12_governance import (
    ACTIVATION_MODE_REPLACE_ACTIVE,
    MODEL_FAMILY,
    SLEEVE_TARGET_FIELDS,
    Stage12Paths,
    _portfolio_family,
    _validate_preview_rows,
    machinery_portfolio_policy_fingerprint,
    portfolio_activation_fingerprint,
    production_preview_rows,
    validate_stage12_lock,
)
from industrials.machinery.stage8_calibration import stage8_paths, utc_now
from industrials.machinery.stage9_backtest import (
    PARITY_FIELDS,
    PRODUCTION_SELECTION_POLICY_VERSION,
    StrategySpec,
    build_production_policy_parity,
)
from portfolio_layer.scores.adapters import run_adapter


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PROTOCOL_VERSION = "machinery_oos_v1.4.1_operational_amendment"
DEFAULT_PROTOCOL_PATH = PACKAGE_ROOT / "model_protocols" / f"{PROTOCOL_VERSION}.json"
DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT / "output" / "industrials" / "machinery" / "v141_operational"
)
TURNOVER_FIELDS = (
    "model",
    "horizon_days",
    "period_count",
    "recurring_period_count",
    "initial_formation_turnover",
    "average_turnover_including_formation",
    "average_recurring_one_way_turnover",
    "maximum_recurring_one_way_turnover",
    "transaction_cost_reconciliation_status",
)
GATE_FIELDS = (
    "gate_id",
    "horizon_days",
    "actual",
    "threshold",
    "direction",
    "status",
    "detail",
)


@dataclass(frozen=True)
class AmendmentPaths:
    root: Path
    acceptance_json: Path
    turnover_csv: Path
    parity_csv: Path
    gates_csv: Path
    run_manifest_json: Path
    validation_json: Path
    source_root: Path
    stage12_root: Path


def amendment_paths(root: Path = DEFAULT_OUTPUT_ROOT) -> AmendmentPaths:
    return AmendmentPaths(
        root=root,
        acceptance_json=root / "review" / "amendment_acceptance.json",
        turnover_csv=root / "review" / "recurring_turnover.csv",
        parity_csv=root / "review" / "production_policy_parity.csv",
        gates_csv=root / "review" / "amendment_gates.csv",
        run_manifest_json=root / "review" / "amendment_run_manifest.json",
        validation_json=root / "review" / "amendment_validation.json",
        source_root=root / "current_source",
        stage12_root=root / "stage12_candidate",
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(path, json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")


def load_amendment_protocol(path: Path = DEFAULT_PROTOCOL_PATH) -> dict[str, Any]:
    payload = _load_json(path)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unexpected machinery operational amendment version")
    if payload.get("model_family") != MODEL_FAMILY:
        raise ValueError("Operational amendment model family is not machinery")
    if payload.get("original_conditional_protocol") != CONDITIONAL_PROTOCOL_VERSION:
        raise ValueError("Operational amendment references the wrong lockbox")
    return payload


def _gate_row(
    gate_id: str,
    *,
    actual: float,
    threshold: float,
    direction: str,
    horizon: int | None = None,
    detail: str = "",
) -> dict[str, str]:
    if direction == "minimum":
        passed = actual >= threshold
    elif direction == "maximum":
        passed = actual <= threshold
    elif direction == "exact":
        passed = abs(actual - threshold) <= 1e-12
    else:
        raise ValueError(f"Unknown amendment gate direction: {direction}")
    return {
        "gate_id": gate_id,
        "horizon_days": str(horizon or ""),
        "actual": f"{actual:.12g}",
        "threshold": f"{threshold:.12g}",
        "direction": direction,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
    }


def _verify_original_lockbox(
    *,
    conditional_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    paths = conditional_paths(conditional_root)
    for path in (
        paths.open_event,
        paths.acceptance_json,
        paths.run_manifest_json,
        paths.periods_csv,
        paths.holdings_csv,
        paths.gates_csv,
    ):
        if not path.is_file():
            raise FileNotFoundError(f"Original conditional artifact is missing: {path}")
    acceptance = _load_json(paths.acceptance_json)
    manifest = _load_json(paths.run_manifest_json)
    event = _load_json(paths.open_event)
    if (
        event.get("state") != "OPENED_COMPLETE"
        or event.get("lockbox_spent") is not True
        or acceptance.get("lockbox_spent") is not True
        or acceptance.get("lockbox_outcomes_accessed") is not True
    ):
        raise ValueError("Original machinery lockbox is not completed and spent")
    if (
        acceptance.get("conditional_promotion_status")
        != "BLOCKED_KEEP_ACTIVE_MODEL"
        or acceptance.get("production_promotion_performed") is not False
    ):
        raise ValueError("Original conditional result is not the expected blocked result")
    if manifest.get("protocol_definition_sha256") != file_sha256(
        CONDITIONAL_PROTOCOL_PATH
    ):
        raise ValueError("Original conditional protocol hash mismatch")
    for metadata in manifest.get("files", {}).values():
        path = Path(str(metadata.get("path") or ""))
        if not path.is_file() or file_sha256(path) != metadata.get("sha256"):
            raise ValueError(f"Original conditional artifact hash mismatch: {path}")
    return acceptance, manifest


def recurring_turnover_diagnostics(
    period_rows: Sequence[Mapping[str, Any]],
    *,
    transaction_cost_rate: float,
) -> list[dict[str, Any]]:
    """Measure recurring turnover while retaining formation-period costs."""
    grouped: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in period_rows:
        model = str(row.get("model") or "")
        horizon = int(str(row.get("horizon_days") or "0"))
        if model in {"stage8_candidate", "active_model"} and horizon in {21, 63}:
            grouped[(model, horizon)].append(row)
    expected = {
        ("stage8_candidate", 21),
        ("stage8_candidate", 63),
        ("active_model", 21),
        ("active_model", 63),
    }
    if set(grouped) != expected:
        raise ValueError("Conditional periods do not cover both models and horizons")
    output: list[dict[str, Any]] = []
    for (model, horizon), rows in sorted(grouped.items()):
        ordered = sorted(rows, key=lambda row: str(row.get("asof_date") or ""))
        if len(ordered) < 2:
            raise ValueError(f"No recurring turnover observation for {model} {horizon}d")
        turnovers: list[float] = []
        costs_ok = True
        for row in ordered:
            turnover = float(str(row.get("one_way_turnover") or "nan"))
            traded = float(str(row.get("traded_notional_fraction") or "nan"))
            cost = float(str(row.get("transaction_cost") or "nan"))
            if not all(math.isfinite(value) for value in (turnover, traded, cost)):
                raise ValueError("Conditional turnover row contains non-finite values")
            turnovers.append(turnover)
            costs_ok = costs_ok and math.isclose(
                cost,
                traded * transaction_cost_rate,
                rel_tol=0.0,
                abs_tol=1e-10,
            )
        if not math.isclose(turnovers[0], 1.0, rel_tol=0.0, abs_tol=1e-10):
            raise ValueError(
                f"First {model} {horizon}d period is not portfolio formation"
            )
        recurring = turnovers[1:]
        output.append(
            {
                "model": model,
                "horizon_days": horizon,
                "period_count": len(turnovers),
                "recurring_period_count": len(recurring),
                "initial_formation_turnover": turnovers[0],
                "average_turnover_including_formation": sum(turnovers)
                / len(turnovers),
                "average_recurring_one_way_turnover": sum(recurring)
                / len(recurring),
                "maximum_recurring_one_way_turnover": max(recurring),
                "transaction_cost_reconciliation_status": (
                    "PASS" if costs_ok else "FAIL"
                ),
            }
        )
    return output


def _lockbox_parity(
    config: dict[str, Any],
    *,
    panel_rows: Sequence[Mapping[str, str]],
    period_rows: Sequence[Mapping[str, Any]],
    holding_rows: Sequence[Mapping[str, Any]],
    candidate_weights: Mapping[str, float],
) -> list[dict[str, Any]]:
    """Reuse production parity logic with the lockbox split normalized."""
    normalized_panel = [
        {**dict(row), "split_name": "holdout"} for row in panel_rows
    ]
    normalized_periods = [
        {**dict(row), "split_name": "holdout"} for row in period_rows
    ]
    normalized_holdings = [
        {**dict(row), "split_name": "holdout"} for row in holding_rows
    ]
    parity = build_production_policy_parity(
        config,
        panel_rows=normalized_panel,
        period_rows=normalized_periods,
        holding_rows=normalized_holdings,
        model_weights=candidate_weights,
        spec=StrategySpec(
            name="long_only_q20_equal",
            portfolio_type="long_only",
            weighting="equal",
            quantile=0.20,
        ),
        horizon=21,
    )
    return [{**row, "split_name": "lockbox"} for row in parity]


def assess_operational_amendment(
    config: dict[str, Any],
    *,
    approval_token: str,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    conditional_root: Path = CONDITIONAL_OUTPUT_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    protocol = load_amendment_protocol(protocol_path)
    expected_token = str(protocol["approval"]["operator_token"])
    if approval_token != expected_token:
        raise PermissionError("Explicit operational amendment token is invalid")
    paths = amendment_paths(output_root)
    if paths.acceptance_json.is_file() and paths.run_manifest_json.is_file():
        validation = validate_operational_amendment(
            protocol_path=protocol_path,
            output_root=output_root,
        )
        if validation["acceptance"] == "PASS":
            return _load_json(paths.acceptance_json)
        raise ValueError("Existing operational amendment artifacts are invalid")
    original, original_manifest = _verify_original_lockbox(
        conditional_root=conditional_root
    )
    conditional = conditional_paths(conditional_root)
    gate_rows = read_rows(conditional.gates_csv)
    failed_rows = [row for row in gate_rows if row.get("status") != "PASS"]
    allowed_ids = {
        str(value)
        for value in protocol["decision_contract"][
            "allowed_original_failed_gate_ids"
        ]
    }
    allowed_horizons = {
        int(value)
        for value in protocol["decision_contract"][
            "allowed_original_failed_horizons"
        ]
    }
    failure_contract_pass = bool(failed_rows) and all(
        row.get("gate_id") in allowed_ids
        and int(str(row.get("horizon_days") or "0")) in allowed_horizons
        for row in failed_rows
    )
    frozen = _load_json(conditional.freeze_manifest)
    candidate_weights = _float_map(
        frozen.get("candidate_weights"),
        name="frozen conditional candidate weights",
    )
    period_rows = read_rows(conditional.periods_csv)
    turnover = recurring_turnover_diagnostics(
        period_rows,
        transaction_cost_rate=20.0 / 10_000.0,
    )
    panel_rows = read_rows(stage8_paths(conditional.panel_root).panel_csv)
    parity = _lockbox_parity(
        config,
        panel_rows=panel_rows,
        period_rows=period_rows,
        holding_rows=read_rows(conditional.holdings_csv),
        candidate_weights=candidate_weights,
    )
    maximum_turnover = float(
        protocol["maximum_average_recurring_one_way_turnover"]
    )
    amended_gates = [
        _gate_row(
            "original_failure_contract",
            actual=1.0 if failure_contract_pass else 0.0,
            threshold=1.0,
            direction="exact",
            detail=";".join(
                f"{row.get('gate_id')}:{row.get('horizon_days')}"
                for row in failed_rows
            ),
        ),
        _gate_row(
            "nonempty_production_policy_parity",
            actual=float(len(parity)),
            threshold=float(
                protocol["decision_contract"][
                    "minimum_nonempty_parity_periods"
                ]
            ),
            direction="minimum",
        ),
        _gate_row(
            "production_policy_parity_failures",
            actual=float(
                sum(row.get("parity_status") != "PASS" for row in parity)
            ),
            threshold=0.0,
            direction="exact",
        ),
    ]
    for row in turnover:
        model = str(row["model"])
        horizon = int(row["horizon_days"])
        amended_gates.append(
            _gate_row(
                f"{model}_average_recurring_one_way_turnover",
                actual=float(row["average_recurring_one_way_turnover"]),
                threshold=maximum_turnover,
                direction="maximum",
                horizon=horizon,
            )
        )
        amended_gates.append(
            _gate_row(
                f"{model}_transaction_cost_reconciliation",
                actual=(
                    1.0
                    if row["transaction_cost_reconciliation_status"] == "PASS"
                    else 0.0
                ),
                threshold=1.0,
                direction="exact",
                horizon=horizon,
            )
        )
    ready = all(row["status"] == "PASS" for row in amended_gates)
    write_csv_atomic(paths.turnover_csv, TURNOVER_FIELDS, turnover)
    write_csv_atomic(paths.parity_csv, PARITY_FIELDS, parity)
    write_csv_atomic(paths.gates_csv, GATE_FIELDS, amended_gates)
    acceptance = {
        "acceptance": "PASS",
        "amendment_status": (
            "READY_FOR_STAGE12_PREFLIGHT"
            if ready
            else "BLOCKED_KEEP_ACTIVE_MODEL"
        ),
        "candidate_id": "equal_components",
        "created_at_utc": utc_now(),
        "failed_amendment_gates": [
            row["gate_id"] for row in amended_gates if row["status"] != "PASS"
        ],
        "hard_gate_pass": ready,
        "initial_funding_cost_remains_charged": True,
        "lockbox_result_modified": False,
        "original_conditional_acceptance_sha256": file_sha256(
            conditional.acceptance_json
        ),
        "original_conditional_run_manifest_sha256": file_sha256(
            conditional.run_manifest_json
        ),
        "original_conditional_status": original[
            "conditional_promotion_status"
        ],
        "original_manifest_protocol_sha256": original_manifest[
            "protocol_definition_sha256"
        ],
        "parity_period_count": len(parity),
        "post_lockbox_governance_exception": True,
        "production_promotion_performed": False,
        "protocol_definition_sha256": file_sha256(protocol_path),
        "protocol_version": PROTOCOL_VERSION,
        "recurring_turnover": turnover,
    }
    _write_json(paths.acceptance_json, acceptance)
    artifacts = (
        paths.acceptance_json,
        paths.turnover_csv,
        paths.parity_csv,
        paths.gates_csv,
    )
    manifest = {
        "artifact_family": "machinery_v141_operational_amendment",
        "created_at_utc": utc_now(),
        "lockbox_result_modified": False,
        "original_conditional_run_manifest_sha256": file_sha256(
            conditional.run_manifest_json
        ),
        "production_promotion_performed": False,
        "protocol_definition_sha256": file_sha256(protocol_path),
        "protocol_version": PROTOCOL_VERSION,
        "files": {
            path.name: {"path": str(path), "sha256": file_sha256(path)}
            for path in artifacts
        },
    }
    _write_json(paths.run_manifest_json, manifest)
    validation = validate_operational_amendment(
        protocol_path=protocol_path,
        output_root=output_root,
    )
    if validation["acceptance"] != "PASS":
        raise ValueError("Operational amendment artifacts failed validation")
    return acceptance


def validate_operational_amendment(
    *,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    paths = amendment_paths(output_root)
    issues: list[str] = []
    for path in (paths.acceptance_json, paths.run_manifest_json):
        if not path.is_file():
            issues.append(f"missing operational amendment artifact {path}")
    if issues:
        result = {"acceptance": "FAIL", "issues": issues}
        _write_json(paths.validation_json, result)
        return result
    acceptance = _load_json(paths.acceptance_json)
    manifest = _load_json(paths.run_manifest_json)
    if manifest.get("protocol_definition_sha256") != file_sha256(protocol_path):
        issues.append("operational amendment protocol hash mismatch")
    for metadata in manifest.get("files", {}).values():
        path = Path(str(metadata.get("path") or ""))
        if not path.is_file() or file_sha256(path) != metadata.get("sha256"):
            issues.append(f"operational amendment artifact hash mismatch {path}")
    if acceptance.get("lockbox_result_modified") is not False:
        issues.append("operational amendment claims to modify the lockbox")
    if acceptance.get("post_lockbox_governance_exception") is not True:
        issues.append("operational amendment classification is missing")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "amendment_status": acceptance.get("amendment_status"),
        "hard_gate_pass": acceptance.get("hard_gate_pass"),
        "issues": issues,
    }
    _write_json(paths.validation_json, result)
    return result


def build_operational_stage12_candidate(
    config: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    asof: str,
    source_dashboard_dir: Path,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    conditional_root: Path = CONDITIONAL_OUTPUT_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    protocol = load_amendment_protocol(protocol_path)
    paths = amendment_paths(output_root)
    validation = validate_operational_amendment(
        protocol_path=protocol_path,
        output_root=output_root,
    )
    if validation.get("acceptance") != "PASS":
        raise ValueError("Operational amendment evidence is invalid")
    acceptance = _load_json(paths.acceptance_json)
    if acceptance.get("amendment_status") != "READY_FOR_STAGE12_PREFLIGHT":
        raise ValueError("Operational amendment gates did not pass")
    stage12_paths = Stage12Paths(paths.stage12_root)
    if stage12_paths.lock_json.exists():
        current = validate_stage12_lock(output_root=paths.stage12_root)
        if current.get("acceptance") == "PASS":
            return _load_json(stage12_paths.lock_json)
        raise FileExistsError("Existing amended Stage 12 candidate is invalid")
    source_rank = source_dashboard_dir / "machinery_final_rank_table.csv"
    source_manifest = (
        source_dashboard_dir / "machinery_final_rank_table_manifest.json"
    )
    if not source_rank.is_file() or not source_manifest.is_file():
        raise FileNotFoundError("Current-schema source dashboard is incomplete")
    source_metadata = _load_json(source_manifest)
    if (
        source_metadata.get("acceptance") != "PASS"
        or source_metadata.get("asof_date") != asof
        or source_metadata.get("rank_table_sha256") != file_sha256(source_rank)
        or source_metadata.get("production_policy_active") is not False
    ):
        raise ValueError("Current-schema source dashboard manifest is invalid")
    source_rows = read_rows(source_rank)
    source_issues = validate_rank_rows(
        source_rows,
        asof=asof,
        allow_production=False,
    )
    if source_issues:
        raise ValueError(
            "Current-schema source dashboard failed: "
            + ";".join(source_issues[:20])
        )
    portfolio_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio = load_yaml(portfolio_path)
    family = _portfolio_family(portfolio)
    cap = float(
        cfg_get(portfolio, "optimizer.sector_weight_caps.machinery", -1.0)
    )
    approved_cap = float(protocol["maximum_portfolio_cap"])
    fixed_equal = {
        str(value)
        for value in cfg_get(
            portfolio,
            "optimizer.fixed_equal_weight_sleeves",
            [],
        )
    }
    if (
        family.get("required") is not True
        or cap != approved_cap
        or MODEL_FAMILY not in fixed_equal
    ):
        raise ValueError(
            "Operational amendment cannot alter live machinery settings"
        )
    conditional = conditional_paths(conditional_root)
    frozen = _load_json(conditional.freeze_manifest)
    candidate_weights = _float_map(
        frozen.get("candidate_weights"),
        name="operational amendment candidate weights",
    )
    spec = StrategySpec(
        name="long_only_q20_equal",
        portfolio_type="long_only",
        weighting="equal",
        quantile=0.20,
    )
    selection_policy = {
        "version": PRODUCTION_SELECTION_POLICY_VERSION,
        "variant": spec.name,
        "portfolio_type": spec.portfolio_type,
        "weighting": spec.weighting,
        "quantile": spec.quantile,
        "minimum_positions": 10,
        "universe_policy": "operating_only",
        "parity_period_count": int(acceptance["parity_period_count"]),
        "parity_status": "PASS",
        "conditional_status": "OPERATIONAL_AMENDMENT_V1_4_1",
    }
    preview = production_preview_rows(
        source_rows,
        weights=candidate_weights,
        asof=asof,
        lock_date="2026-01-01",
        score_model_version=str(
            cfg_get(config, "machinery_stage12.score_model_version")
        ),
        model_version=str(cfg_get(config, "machinery_stage12.model_version")),
        scoring_contract_version=str(
            cfg_get(config, "machinery_stage12.scoring_contract_version")
        ),
        selection_spec=spec,
        minimum_positions=int(selection_policy["minimum_positions"]),
        universe_policy=str(selection_policy["universe_policy"]),
    )
    preview_issues = _validate_preview_rows(
        preview,
        asof=asof,
        selection_policy=selection_policy,
    )
    if preview_issues:
        raise ValueError(
            "Amended production preview failed: " + ";".join(preview_issues)
        )
    paths.stage12_root.mkdir(parents=True, exist_ok=True)
    write_rank_rows(stage12_paths.preview_csv, preview)
    sleeve_rows = [
        {field: row.get(field, "") for field in SLEEVE_TARGET_FIELDS}
        for row in preview
        if row["portfolio_universe_eligible_flag"] == "1"
    ]
    write_csv_atomic(
        stage12_paths.sleeve_targets_csv,
        SLEEVE_TARGET_FIELDS,
        sleeve_rows,
    )
    adapter_config = dict(family)
    adapter_config["file_mode"] = "flat"
    adapter_config["file_path"] = str(stage12_paths.preview_csv)
    adapter_result = run_adapter(
        adapter_config,
        resolve_path(
            cfg_get(portfolio, "score_contract.sector_output_root"),
            base_dir=portfolio_path.parent,
        ),
        asof,
    )
    selected = {
        row["ticker"]
        for row in preview
        if row["portfolio_sleeve_selected_flag"] == "1"
    }
    adapted = {
        row.ticker for row in adapter_result.rows if row.investable_eligible == 1
    }
    if adapted != selected:
        raise ValueError(
            "Portfolio adapter changed amended machinery membership"
        )
    active = _active_evidence(config, config_path=config_path)
    active_state = Path(str(active["activation_state_path"]))
    panel_manifest = stage8_paths(conditional.panel_root).panel_manifest_json
    lock = {
        "acceptance": "PASS",
        "activation_mode": ACTIVATION_MODE_REPLACE_ACTIVE,
        "activation_requires_explicit_operator_approval": True,
        "active_activation_state": str(active_state),
        "adapter_investable_count": len(adapted),
        "amendment_acceptance": str(paths.acceptance_json),
        "amendment_acceptance_sha256": file_sha256(paths.acceptance_json),
        "amendment_protocol_sha256": file_sha256(protocol_path),
        "broad_portfolio_universe_eligible_count": len(sleeve_rows),
        "created_at_utc": utc_now(),
        "current_portfolio_cap": cap,
        "development_end_date": "2025-12-31",
        "live_dashboard_modified": False,
        "lockbox_start_date": "2026-01-01",
        "machinery_portfolio_policy_sha256": (
            machinery_portfolio_policy_fingerprint(portfolio)
        ),
        "minimum_capacity_multiple": 5.0,
        "model_family": MODEL_FAMILY,
        "portfolio_adapter_sha256": file_sha256(
            PROJECT_ROOT / "portfolio_layer" / "scores" / "adapters.py"
        ),
        "portfolio_config_sha256": file_sha256(portfolio_path),
        "portfolio_equal_weight_constraint_configured": True,
        "portfolio_non_activation_config_sha256": (
            portfolio_activation_fingerprint(portfolio)
        ),
        "portfolio_optimizer_sha256": file_sha256(
            PROJECT_ROOT
            / "portfolio_layer"
            / "optimizer"
            / "optimizer_core.py"
        ),
        "portfolio_optimizer_runner_sha256": file_sha256(
            PROJECT_ROOT
            / "portfolio_layer"
            / "optimizer"
            / "09_run_portfolio_optimizer.py"
        ),
        "post_lockbox_governance_exception": True,
        "previous_activation_state_sha256": file_sha256(active_state),
        "production_promotion_performed": False,
        "production_selection_policy": {
            **selection_policy,
            "governance_candidate_position_count": len(selected),
        },
        "production_start_date": asof,
        "promotion_candidate_asof": asof,
        "proposed_portfolio_candidate_count": len(selected),
        "proposed_portfolio_cap": approved_cap,
        "recommended_model": "v141_equal_components_operational_amendment",
        "recommended_variant": spec.name,
        "recommended_weights": candidate_weights,
        "selected_sleeve_count": len(selected),
        "selected_sleeve_target_weight_sum": sum(
            float(row["portfolio_sleeve_target_weight"])
            for row in preview
            if row["portfolio_sleeve_selected_flag"] == "1"
        ),
        "source_dashboard_asof": asof,
        "source_dashboard_manifest": str(source_manifest),
        "source_dashboard_manifest_sha256": file_sha256(source_manifest),
        "source_dashboard_rank": str(source_rank),
        "source_dashboard_sha256": file_sha256(source_rank),
        "stage12_status": "READY_NOT_ACTIVATED",
        "stage8_run_manifest": str(panel_manifest),
        "stage8_run_manifest_sha256": file_sha256(panel_manifest),
        "stage9_run_manifest": str(paths.run_manifest_json),
        "stage9_run_manifest_sha256": file_sha256(paths.run_manifest_json),
        "target_aum_usd": 300000.0,
    }
    write_json_atomic(stage12_paths.lock_json, lock)
    artifacts = (
        stage12_paths.preview_csv,
        stage12_paths.sleeve_targets_csv,
        stage12_paths.lock_json,
    )
    write_json_atomic(
        stage12_paths.manifest_json,
        {
            "artifact_family": (
                "machinery_v141_operational_stage12_candidate"
            ),
            "created_at_utc": utc_now(),
            "production_promotion_performed": False,
            "files": {
                path.name: {
                    "path": str(path),
                    "sha256": file_sha256(path),
                }
                for path in artifacts
            },
        },
    )
    stage12_validation = validate_stage12_lock(
        output_root=paths.stage12_root
    )
    if stage12_validation.get("acceptance") != "PASS":
        raise ValueError("Amended Stage 12 candidate failed validation")
    return lock
