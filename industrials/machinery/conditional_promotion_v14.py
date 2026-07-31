"""One-time conditional promotion controls for machinery v1.4."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from industrials.core.config import cfg_get, load_yaml, resolve_path
from industrials.core.reports import write_csv_atomic, write_text_atomic
from industrials.machinery.confirmatory_v14 import (
    DEFAULT_PROTOCOL_PATH as V14_PROTOCOL_PATH,
)
from industrials.machinery.confirmatory_v14 import (
    DEFAULT_V14_ROOT,
    confirmatory_paths,
    load_protocol_definition,
)
from industrials.machinery.scoring import (
    file_sha256,
    read_rows,
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
from industrials.machinery.stage8_calibration import (
    COMPONENT_FIELDS,
    build_panel,
    evaluate_weights,
    parse_date,
    stage8_paths,
    utc_now,
)
from industrials.machinery.stage9_backtest import (
    HOLDING_FIELDS,
    PARITY_FIELDS,
    PERIOD_FIELDS,
    PRODUCTION_SELECTION_POLICY_VERSION,
    SUMMARY_FIELDS,
    StrategySpec,
    build_production_policy_parity,
    run_variant,
    summarize_variant,
)
from portfolio_layer.scores.adapters import run_adapter


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
PROTOCOL_VERSION = "machinery_oos_v1.4.0_conditional_promotion"
DEFAULT_PROTOCOL_PATH = (
    PACKAGE_ROOT / "model_protocols" / f"{PROTOCOL_VERSION}.json"
)
DEFAULT_OUTPUT_ROOT = DEFAULT_V14_ROOT / "conditional_promotion"
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.yaml"
GATE_FIELDS = (
    "gate_id",
    "horizon_days",
    "actual",
    "threshold",
    "direction",
    "status",
    "detail",
)
COMPARISON_FIELDS = (
    "horizon_days",
    "candidate_mean_net_excess",
    "active_mean_net_excess",
    "candidate_minus_active",
    "candidate_fixed_cap_marginal_net_alpha",
    "active_fixed_cap_marginal_net_alpha",
    "fixed_cap_marginal_net_alpha_improvement",
    "candidate_confidence_bound_advisory",
)


@dataclass(frozen=True)
class ConditionalPaths:
    root: Path
    freeze_manifest: Path
    freeze_validation: Path
    open_event: Path
    panel_root: Path
    periods_csv: Path
    holdings_csv: Path
    summary_csv: Path
    parity_csv: Path
    comparisons_csv: Path
    gates_csv: Path
    acceptance_json: Path
    run_manifest_json: Path
    validation_json: Path
    stage12_root: Path


def conditional_paths(root: Path = DEFAULT_OUTPUT_ROOT) -> ConditionalPaths:
    return ConditionalPaths(
        root=root,
        freeze_manifest=root / "protocol" / "conditional_freeze_manifest.json",
        freeze_validation=root / "protocol" / "conditional_freeze_validation.json",
        open_event=root / "lockbox" / "lockbox_open_event.json",
        panel_root=root / "lockbox" / "panel",
        periods_csv=root / "lockbox" / "conditional_periods.csv",
        holdings_csv=root / "lockbox" / "conditional_holdings.csv",
        summary_csv=root / "lockbox" / "conditional_summary.csv",
        parity_csv=root / "lockbox" / "conditional_policy_parity.csv",
        comparisons_csv=root / "lockbox" / "conditional_model_comparison.csv",
        gates_csv=root / "lockbox" / "conditional_gate_results.csv",
        acceptance_json=root / "lockbox" / "conditional_acceptance.json",
        run_manifest_json=root / "lockbox" / "conditional_run_manifest.json",
        validation_json=root / "lockbox" / "conditional_validation.json",
        stage12_root=root / "stage12_candidate",
    )


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(dict(payload), indent=2, sort_keys=True) + "\n",
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _float_map(raw: object, *, name: str) -> dict[str, float]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{name} must be a mapping")
    result = {str(key): float(value) for key, value in raw.items()}
    if set(result) != set(COMPONENT_FIELDS):
        raise ValueError(f"{name} does not cover the machinery components")
    if any(not math.isfinite(value) or value < 0 for value in result.values()):
        raise ValueError(f"{name} contains invalid weights")
    if abs(sum(result.values()) - 1.0) > 1e-9:
        raise ValueError(f"{name} must sum to one")
    return result


def load_conditional_protocol(
    path: Path = DEFAULT_PROTOCOL_PATH,
) -> dict[str, Any]:
    payload = _load_json(path)
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unexpected conditional protocol version")
    if payload.get("model_family") != MODEL_FAMILY:
        raise ValueError("Conditional protocol model family is not machinery")
    if payload.get("candidate_id") != "equal_components":
        raise ValueError("Conditional protocol must use equal_components")
    decision = payload.get("decision_policy")
    if not isinstance(decision, Mapping):
        raise ValueError("Conditional decision policy is missing")
    if decision.get("confidence_bound_role") != "advisory_only":
        raise ValueError("The conditional confidence bound must be advisory")
    if decision.get("stage8_v1_3_result_is_not_overridden") is not True:
        raise ValueError("The conditional policy must preserve v1.3 history")
    window = payload.get("evidence_window")
    if not isinstance(window, Mapping):
        raise ValueError("Conditional evidence window is missing")
    start = parse_date(str(window.get("sealed_start_date") or ""))
    end = parse_date(str(window.get("end_date") or ""))
    if start != parse_date("2026-01-01") or end < start:
        raise ValueError("Conditional evidence window is invalid")
    evaluation = payload.get("evaluation_contract")
    if not isinstance(evaluation, Mapping):
        raise ValueError("Conditional evaluation contract is missing")
    if evaluation.get("return_basis") != "next_session_open_execution_excess":
        raise ValueError("Conditional return basis changed")
    if evaluation.get("production_universe_policy") != "operating_only":
        raise ValueError("Conditional universe must remain operating-only")
    horizons = [int(value) for value in evaluation.get("horizons_trading_days", [])]
    if horizons != [21, 63]:
        raise ValueError("Conditional horizons must be 21 and 63 days")
    approval = payload.get("approval")
    if not isinstance(approval, Mapping) or not str(
        approval.get("lockbox_open_token") or ""
    ):
        raise ValueError("Conditional lockbox approval token is missing")
    return payload


def _active_evidence(
    config: dict[str, Any],
    *,
    config_path: Path,
) -> dict[str, Any]:
    active_root = resolve_path(
        cfg_get(config, "machinery_stage12.output_root"),
        base_dir=config_path.parent,
    )
    state_path = Stage12Paths(active_root).activation_state_json
    if not state_path.is_file():
        raise FileNotFoundError("Active machinery state is missing")
    state = _load_json(state_path)
    if (
        state.get("acceptance") != "PASS"
        or state.get("production_policy_status") != "ACTIVE"
    ):
        raise ValueError("Machinery production state is not active")
    lock_path = Path(str(state.get("governance_lock") or ""))
    if not lock_path.is_file():
        raise FileNotFoundError("Active machinery governance lock is missing")
    if file_sha256(lock_path) != str(state.get("governance_lock_sha256") or ""):
        raise ValueError("Active machinery governance lock changed")
    lock = _load_json(lock_path)
    weights = _float_map(lock.get("recommended_weights"), name="active weights")
    return {
        "active_root": str(active_root),
        "activation_asof": state.get("activation_asof"),
        "activation_state_path": str(state_path),
        "activation_state_sha256": file_sha256(state_path),
        "governance_lock_path": str(lock_path),
        "governance_lock_sha256": file_sha256(lock_path),
        "portfolio_cap": float(state.get("portfolio_cap") or 0.0),
        "weights": weights,
    }


def _governed_source_hashes() -> dict[str, str]:
    paths = {
        "conditional_promotion_v14.py": Path(__file__).resolve(),
        "confirmatory_v14.py": PACKAGE_ROOT / "confirmatory_v14.py",
        "stage8_calibration.py": PACKAGE_ROOT / "stage8_calibration.py",
        "stage9_backtest.py": PACKAGE_ROOT / "stage9_backtest.py",
        "stage12_governance.py": PACKAGE_ROOT / "stage12_governance.py",
        "stage12_activation.py": PACKAGE_ROOT / "stage12_activation.py",
        "stage12_activation_transaction.py": (
            PACKAGE_ROOT / "stage12_activation_transaction.py"
        ),
        "portfolio_adapters.py": (
            PROJECT_ROOT / "portfolio_layer" / "scores" / "adapters.py"
        ),
        "portfolio_optimizer_core.py": (
            PROJECT_ROOT / "portfolio_layer" / "optimizer" / "optimizer_core.py"
        ),
        "portfolio_optimizer_runner.py": (
            PROJECT_ROOT
            / "portfolio_layer"
            / "optimizer"
            / "09_run_portfolio_optimizer.py"
        ),
    }
    return {name: file_sha256(path) for name, path in paths.items()}


def freeze_conditional_protocol(
    config: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    protocol = load_conditional_protocol(protocol_path)
    candidate = load_protocol_definition(V14_PROTOCOL_PATH)
    candidate_weights = _float_map(
        candidate.get("weights"),
        name="candidate weights",
    )
    paths = conditional_paths(output_root)
    if paths.freeze_manifest.exists():
        result = validate_conditional_freeze(
            config,
            config_path=config_path,
            protocol_path=protocol_path,
            output_root=output_root,
        )
        if result["acceptance"] == "PASS":
            return result
        raise FileExistsError("Conditional freeze exists but is no longer valid")
    v14_freeze = confirmatory_paths(DEFAULT_V14_ROOT).freeze_manifest
    if (
        not v14_freeze.is_file()
        or _load_json(v14_freeze).get("acceptance") != "PASS"
    ):
        raise ValueError("The fixed v1.4 candidate is not frozen")
    active = _active_evidence(config, config_path=config_path)
    portfolio_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio = load_yaml(portfolio_path)
    family = _portfolio_family(portfolio)
    configured_cap = float(
        cfg_get(portfolio, "optimizer.sector_weight_caps.machinery", -1.0)
    )
    maximum_cap = float(
        protocol["portfolio_contract"]["maximum_provisional_cap"]
    )
    if family.get("required") is not True or configured_cap != maximum_cap:
        raise ValueError("The active portfolio machinery sleeve is not at its sealed cap")
    if float(active["portfolio_cap"]) != maximum_cap:
        raise ValueError("The active machinery state cap differs from the protocol")
    payload = {
        "acceptance": "PASS",
        "artifact_family": "machinery_conditional_protocol_freeze",
        "active_model": active,
        "candidate_protocol_path": str(V14_PROTOCOL_PATH.resolve()),
        "candidate_protocol_sha256": file_sha256(V14_PROTOCOL_PATH),
        "candidate_v14_freeze_path": str(v14_freeze.resolve()),
        "candidate_v14_freeze_sha256": file_sha256(v14_freeze),
        "candidate_weights": candidate_weights,
        "created_at_utc": utc_now(),
        "governed_source_sha256": _governed_source_hashes(),
        "lockbox_outcomes_accessed": False,
        "portfolio_config_path": str(portfolio_path.resolve()),
        "portfolio_config_sha256": file_sha256(portfolio_path),
        "production_cap_increase_permitted": False,
        "production_promotion_performed": False,
        "protocol_definition_path": str(protocol_path.resolve()),
        "protocol_definition_sha256": file_sha256(protocol_path),
        "protocol_version": PROTOCOL_VERSION,
        "stage8_v1_3_result_overridden": False,
    }
    _write_json(paths.freeze_manifest, payload)
    return validate_conditional_freeze(
        config,
        config_path=config_path,
        protocol_path=protocol_path,
        output_root=output_root,
    )


def validate_conditional_freeze(
    config: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    paths = conditional_paths(output_root)
    issues: list[str] = []
    try:
        load_conditional_protocol(protocol_path)
    except (FileNotFoundError, ValueError) as exc:
        issues.append(str(exc))
    if not paths.freeze_manifest.is_file():
        payload: dict[str, Any] = {}
        issues.append("conditional freeze manifest is missing")
    else:
        payload = _load_json(paths.freeze_manifest)
    if payload.get("protocol_definition_sha256") != file_sha256(protocol_path):
        issues.append("conditional protocol changed after freeze")
    if payload.get("candidate_protocol_sha256") != file_sha256(V14_PROTOCOL_PATH):
        issues.append("fixed v1.4 candidate protocol changed")
    if payload.get("governed_source_sha256") != _governed_source_hashes():
        issues.append("conditional governed source changed after freeze")
    try:
        active = _active_evidence(config, config_path=config_path)
        frozen_active = payload.get("active_model")
        if not isinstance(frozen_active, Mapping):
            issues.append("frozen active model evidence is missing")
        elif any(
            frozen_active.get(field) != active.get(field)
            for field in (
                "activation_state_sha256",
                "governance_lock_sha256",
                "portfolio_cap",
                "weights",
            )
        ):
            issues.append("active machinery model changed after conditional freeze")
    except (FileNotFoundError, ValueError) as exc:
        issues.append(str(exc))
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "artifact_family": "machinery_conditional_freeze_validation",
        "lockbox_outcomes_accessed": False,
        "production_promotion_performed": False,
        "protocol_version": PROTOCOL_VERSION,
        "issues": issues,
    }
    _write_json(paths.freeze_validation, result)
    return result


def _begin_open_event(
    *,
    path: Path,
    protocol_sha256: str,
    freeze_sha256: str,
    token: str,
) -> dict[str, Any]:
    if path.exists():
        existing = _load_json(path)
        if existing.get("protocol_definition_sha256") != protocol_sha256:
            raise ValueError("Existing lockbox open event uses another protocol")
        if existing.get("freeze_manifest_sha256") != freeze_sha256:
            raise ValueError("Existing lockbox open event uses another freeze")
        if existing.get("approval_token_sha256") != _sha256_text(token):
            raise PermissionError("Lockbox resume token does not match")
        return existing
    payload = {
        "approval_token_sha256": _sha256_text(token),
        "artifact_family": "machinery_conditional_lockbox_open_event",
        "freeze_manifest_sha256": freeze_sha256,
        "lockbox_outcomes_accessed": True,
        "lockbox_spent": True,
        "opened_at_utc": utc_now(),
        "production_promotion_performed": False,
        "protocol_definition_sha256": protocol_sha256,
        "protocol_version": PROTOCOL_VERSION,
        "state": "OPEN_IN_PROGRESS",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return payload


def _lockbox_config(
    config: dict[str, Any],
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(config)
    stage8 = result.setdefault("machinery_stage8", {})
    if not isinstance(stage8, dict):
        raise ValueError("machinery_stage8 config must be a mapping")
    window = protocol["evidence_window"]
    end = parse_date(str(window["end_date"]))
    stage8["development_start_date"] = str(window["sealed_start_date"])
    stage8["development_end_date"] = end.isoformat()
    stage8["sealed_start_date"] = end.replace(year=end.year + 1).isoformat()
    return result


def _materialize_lockbox_panel(
    config: dict[str, Any],
    *,
    config_path: Path,
    protocol: Mapping[str, Any],
    paths: ConditionalPaths,
    freeze_sha256: str,
) -> tuple[list[dict[str, str]], list[int], dict[str, Any]]:
    panel_paths = stage8_paths(paths.panel_root)
    db_path = resolve_path(
        cfg_get(config, "paths.database_path"),
        base_dir=config_path.parent,
    )
    rows, horizons, manifest = build_panel(
        _lockbox_config(config, protocol),
        config_path=config_path,
        db_path=db_path,
        paths=panel_paths,
    )
    if not rows:
        raise ValueError("Conditional lockbox panel is empty")
    for row in rows:
        row["split_name"] = "lockbox"
    write_csv_atomic(panel_paths.panel_csv, tuple(rows[0]), rows)
    snapshot_dates = sorted({row["asof_date"] for row in rows})
    split_fields = (
        "split_name",
        "start_date",
        "end_date",
        "snapshot_count",
        "role",
    )
    write_csv_atomic(
        panel_paths.splits_csv,
        split_fields,
        [
            {
                "split_name": "lockbox",
                "start_date": snapshot_dates[0],
                "end_date": snapshot_dates[-1],
                "snapshot_count": str(len(snapshot_dates)),
                "role": "one_time_conditional_evaluation",
            }
        ],
    )
    manifest.update(
        {
            "artifact_family": "machinery_conditional_lockbox_panel",
            "freeze_manifest_sha256": freeze_sha256,
            "lockbox_outcomes_accessed": True,
            "lockbox_spent": True,
            "protocol_definition_sha256": file_sha256(DEFAULT_PROTOCOL_PATH),
            "protocol_version": PROTOCOL_VERSION,
            "split_name": "lockbox",
        }
    )
    for target in (
        panel_paths.panel_csv,
        panel_paths.source_index_csv,
        panel_paths.splits_csv,
    ):
        metadata = manifest.setdefault("files", {}).setdefault(target.name, {})
        metadata["sha256"] = file_sha256(target)
    write_json_atomic(panel_paths.panel_manifest_json, manifest)
    return rows, horizons, manifest


def _without_date_rows(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "date_rows"}


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
    elif direction == "exact_zero":
        passed = abs(actual) <= threshold
    else:
        raise ValueError(f"Unknown gate direction: {direction}")
    return {
        "gate_id": gate_id,
        "horizon_days": str(horizon or ""),
        "actual": f"{actual:.12g}",
        "threshold": f"{threshold:.12g}",
        "direction": direction,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
    }


def _horizon_threshold(
    block: Mapping[str, Any],
    name: str,
    horizon: int,
) -> float:
    raw = block.get(name)
    if not isinstance(raw, Mapping):
        raise ValueError(f"Conditional threshold {name} is missing")
    return float(raw[str(horizon)])


def _evaluate_lockbox(
    config: dict[str, Any],
    *,
    protocol: Mapping[str, Any],
    panel_rows: Sequence[Mapping[str, str]],
    horizons: Sequence[int],
    candidate_weights: Mapping[str, float],
    active_weights: Mapping[str, float],
    paths: ConditionalPaths,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    dates = sorted({str(row["asof_date"]) for row in panel_rows})
    candidate_metrics = evaluate_weights(
        config,
        rows=panel_rows,
        dates=dates,
        horizons=horizons,
        weights=candidate_weights,
    )
    active_metrics = evaluate_weights(
        config,
        rows=panel_rows,
        dates=dates,
        horizons=horizons,
        weights=active_weights,
    )
    spec = StrategySpec(
        name="long_only_q20_equal",
        portfolio_type="long_only",
        weighting="equal",
        quantile=0.20,
    )
    period_rows: list[dict[str, Any]] = []
    holding_rows: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    for model, weights in (
        ("stage8_candidate", candidate_weights),
        ("active_model", active_weights),
    ):
        for horizon in horizons:
            periods, holdings = run_variant(
                config,
                rows=panel_rows,
                model=model,
                model_weights=weights,
                spec=spec,
                horizon=horizon,
                split_name="lockbox",
                split_names={"lockbox"},
            )
            period_rows.extend(periods)
            holding_rows.extend(holdings)
            summary = summarize_variant(periods, holdings)
            if summary:
                summary_rows.append(summary)
    parity = build_production_policy_parity(
        config,
        panel_rows=panel_rows,
        period_rows=period_rows,
        holding_rows=holding_rows,
        model_weights=candidate_weights,
        spec=spec,
        horizon=21,
    )
    write_csv_atomic(paths.periods_csv, PERIOD_FIELDS, period_rows)
    write_csv_atomic(paths.holdings_csv, HOLDING_FIELDS, holding_rows)
    write_csv_atomic(paths.summary_csv, SUMMARY_FIELDS, summary_rows)
    write_csv_atomic(paths.parity_csv, PARITY_FIELDS, parity)
    summary_by_key = {
        (str(row["model"]), int(row["horizon_days"])): row
        for row in summary_rows
    }
    hard = protocol["hard_gates"]
    active_comparison = protocol["active_model_comparison"]
    portfolio = protocol["portfolio_contract"]
    cap = float(portfolio["maximum_provisional_cap"])
    gate_rows: list[dict[str, str]] = []
    comparison_rows: list[dict[str, str]] = []
    for horizon in horizons:
        candidate_mean = float(
            candidate_metrics[f"mean_top_excess_net_{horizon}d"]
        )
        active_mean = float(active_metrics[f"mean_top_excess_net_{horizon}d"])
        improvement = candidate_mean - active_mean
        candidate_alpha = cap * candidate_mean
        active_alpha = cap * active_mean
        alpha_improvement = candidate_alpha - active_alpha
        comparison_rows.append(
            {
                "horizon_days": str(horizon),
                "candidate_mean_net_excess": f"{candidate_mean:.12g}",
                "active_mean_net_excess": f"{active_mean:.12g}",
                "candidate_minus_active": f"{improvement:.12g}",
                "candidate_fixed_cap_marginal_net_alpha": (
                    f"{candidate_alpha:.12g}"
                ),
                "active_fixed_cap_marginal_net_alpha": f"{active_alpha:.12g}",
                "fixed_cap_marginal_net_alpha_improvement": (
                    f"{alpha_improvement:.12g}"
                ),
                "candidate_confidence_bound_advisory": str(
                    candidate_metrics.get(
                        f"top_excess_net_lower_confidence_bound_{horizon}d"
                    )
                    or 0.0
                ),
            }
        )
        checks = (
            (
                "weekly_observation_count",
                float(candidate_metrics[f"n_top_dates_{horizon}d"]),
                _horizon_threshold(hard, "minimum_weekly_observations", horizon),
            ),
            (
                "mean_net_excess",
                candidate_mean,
                _horizon_threshold(
                    hard,
                    "minimum_mean_net_excess_return",
                    horizon,
                ),
            ),
            (
                "median_net_excess",
                float(candidate_metrics[f"median_top_excess_net_{horizon}d"]),
                _horizon_threshold(
                    hard,
                    "minimum_median_net_excess_return",
                    horizon,
                ),
            ),
            (
                "net_excess_hit_rate",
                float(candidate_metrics[f"top_excess_hit_rate_{horizon}d"]),
                _horizon_threshold(hard, "minimum_hit_rate", horizon),
            ),
            (
                "non_overlapping_observation_count",
                float(
                    candidate_metrics[
                        f"n_non_overlapping_top_dates_{horizon}d"
                    ]
                ),
                _horizon_threshold(
                    hard,
                    "minimum_non_overlapping_observations",
                    horizon,
                ),
            ),
            (
                "active_model_net_excess_improvement",
                improvement,
                _horizon_threshold(
                    active_comparison,
                    "minimum_mean_net_excess_improvement",
                    horizon,
                ),
            ),
            (
                "fixed_cap_marginal_net_alpha",
                candidate_alpha,
                _horizon_threshold(
                    portfolio,
                    "minimum_fixed_cap_marginal_net_alpha",
                    horizon,
                ),
            ),
            (
                "fixed_cap_marginal_net_alpha_improvement",
                alpha_improvement,
                _horizon_threshold(
                    portfolio,
                    "minimum_fixed_cap_marginal_net_alpha_improvement",
                    horizon,
                ),
            ),
        )
        for gate_id, actual, threshold in checks:
            gate_rows.append(
                _gate_row(
                    gate_id,
                    actual=actual,
                    threshold=threshold,
                    direction="minimum",
                    horizon=horizon,
                )
            )
        summary = summary_by_key.get(("stage8_candidate", horizon))
        if summary is None:
            raise ValueError(f"Candidate summary is missing for {horizon}d")
        risk_checks = (
            (
                "maximum_drawdown",
                float(summary["max_drawdown"]),
                float(hard["maximum_drawdown"]),
                "minimum",
            ),
            (
                "average_one_way_turnover",
                float(summary["average_one_way_turnover"]),
                float(hard["maximum_average_one_way_turnover"]),
                "maximum",
            ),
            (
                "worst_position_weight",
                float(summary["worst_max_position_weight"]),
                float(hard["maximum_position_weight"]),
                "maximum",
            ),
            (
                "worst_selected_cohort_share",
                float(summary["worst_max_cohort_share"]),
                float(hard["maximum_selected_cohort_share"]),
                "maximum",
            ),
            (
                "adv_weight_coverage",
                float(summary["average_adv_weight_coverage"]),
                float(hard["minimum_adv_weight_coverage"]),
                "minimum",
            ),
            (
                "capacity_p10_usd",
                float(summary["capacity_p10_usd"]),
                float(portfolio["target_aum_usd"])
                * float(hard["minimum_capacity_multiple"]),
                "minimum",
            ),
            (
                "selected_cohort_count",
                float(summary["cohort_count"]),
                float(hard["minimum_selected_cohorts"]),
                "minimum",
            ),
        )
        for gate_id, actual, threshold, direction in risk_checks:
            gate_rows.append(
                _gate_row(
                    gate_id,
                    actual=actual,
                    threshold=threshold,
                    direction=direction,
                    horizon=horizon,
                )
            )
    parity_failures = sum(
        str(row.get("parity_status") or "") != "PASS" for row in parity
    )
    gate_rows.append(
        _gate_row(
            "production_policy_parity_failures",
            actual=float(parity_failures),
            threshold=0.0,
            direction="exact_zero",
            detail=f"periods={len(parity)}",
        )
    )
    write_csv_atomic(paths.comparisons_csv, COMPARISON_FIELDS, comparison_rows)
    write_csv_atomic(paths.gates_csv, GATE_FIELDS, gate_rows)
    result = {
        "active_metrics": _without_date_rows(active_metrics),
        "candidate_metrics": _without_date_rows(candidate_metrics),
        "comparison_rows": comparison_rows,
        "gate_count": len(gate_rows),
        "hard_gate_pass": all(row["status"] == "PASS" for row in gate_rows),
        "holding_row_count": len(holding_rows),
        "parity_period_count": len(parity),
        "period_row_count": len(period_rows),
        "summary_rows": summary_rows,
    }
    return result, gate_rows


def open_conditional_lockbox(
    config: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    approval_token: str,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    protocol = load_conditional_protocol(protocol_path)
    expected_token = str(protocol["approval"]["lockbox_open_token"])
    if approval_token != expected_token:
        raise PermissionError("Explicit conditional lockbox token is invalid")
    paths = conditional_paths(output_root)
    if paths.acceptance_json.exists() and paths.run_manifest_json.exists():
        validation = validate_conditional_result(
            protocol_path=protocol_path,
            output_root=output_root,
        )
        if validation["acceptance"] == "PASS":
            return _load_json(paths.acceptance_json)
        raise ValueError("Existing conditional result failed validation")
    freeze = validate_conditional_freeze(
        config,
        config_path=config_path,
        protocol_path=protocol_path,
        output_root=output_root,
    )
    if freeze["acceptance"] != "PASS":
        raise ValueError("Conditional protocol freeze is invalid")
    freeze_sha = file_sha256(paths.freeze_manifest)
    event = _begin_open_event(
        path=paths.open_event,
        protocol_sha256=file_sha256(protocol_path),
        freeze_sha256=freeze_sha,
        token=approval_token,
    )
    if event.get("state") not in {"OPEN_IN_PROGRESS", "OPENED_FAILED"}:
        raise ValueError("Lockbox was already opened without a valid result")
    try:
        panel_rows, horizons, panel_manifest = _materialize_lockbox_panel(
            config,
            config_path=config_path,
            protocol=protocol,
            paths=paths,
            freeze_sha256=freeze_sha,
        )
        frozen = _load_json(paths.freeze_manifest)
        evaluation, gate_rows = _evaluate_lockbox(
            config,
            protocol=protocol,
            panel_rows=panel_rows,
            horizons=horizons,
            candidate_weights=_float_map(
                frozen["candidate_weights"],
                name="frozen candidate weights",
            ),
            active_weights=_float_map(
                frozen["active_model"]["weights"],
                name="frozen active weights",
            ),
            paths=paths,
        )
        failed = [
            row["gate_id"] for row in gate_rows if row["status"] != "PASS"
        ]
        ready = bool(evaluation["hard_gate_pass"])
        acceptance = {
            "acceptance": "PASS",
            "active_model_remains_active": True,
            "artifact_family": "machinery_conditional_lockbox_acceptance",
            "candidate_id": "equal_components",
            "conditional_promotion_status": (
                "READY_FOR_PORTFOLIO_SMOKE"
                if ready
                else "BLOCKED_KEEP_ACTIVE_MODEL"
            ),
            "confidence_bound_role": "advisory_only",
            "created_at_utc": utc_now(),
            "evaluation": evaluation,
            "failed_hard_gates": failed,
            "hard_gate_pass": ready,
            "lockbox_outcomes_accessed": True,
            "lockbox_spent": True,
            "panel_manifest_sha256": file_sha256(
                stage8_paths(paths.panel_root).panel_manifest_json
            ),
            "portfolio_smoke_completed": False,
            "production_cap_increase_performed": False,
            "production_promotion_performed": False,
            "protocol_definition_sha256": file_sha256(protocol_path),
            "protocol_version": PROTOCOL_VERSION,
            "stage8_v1_3_result_overridden": False,
            "snapshot_count": panel_manifest.get("snapshot_count"),
        }
        _write_json(paths.acceptance_json, acceptance)
        panel_paths = stage8_paths(paths.panel_root)
        artifacts = (
            panel_paths.panel_csv,
            panel_paths.source_index_csv,
            panel_paths.splits_csv,
            panel_paths.panel_manifest_json,
            paths.periods_csv,
            paths.holdings_csv,
            paths.summary_csv,
            paths.parity_csv,
            paths.comparisons_csv,
            paths.gates_csv,
            paths.acceptance_json,
        )
        manifest = {
            "artifact_family": "machinery_conditional_lockbox_run",
            "created_at_utc": utc_now(),
            "freeze_manifest_sha256": freeze_sha,
            "lockbox_outcomes_accessed": True,
            "lockbox_spent": True,
            "production_promotion_performed": False,
            "protocol_definition_sha256": file_sha256(protocol_path),
            "protocol_version": PROTOCOL_VERSION,
            "files": {
                path.name: {"path": str(path), "sha256": file_sha256(path)}
                for path in artifacts
            },
        }
        _write_json(paths.run_manifest_json, manifest)
        validation = validate_conditional_result(
            protocol_path=protocol_path,
            output_root=output_root,
        )
        if validation["acceptance"] != "PASS":
            raise ValueError("Conditional lockbox artifacts failed validation")
        completed_event = {
            **event,
            "completed_at_utc": utc_now(),
            "conditional_acceptance_sha256": file_sha256(paths.acceptance_json),
            "conditional_run_manifest_sha256": file_sha256(
                paths.run_manifest_json
            ),
            "decision": acceptance["conditional_promotion_status"],
            "state": "OPENED_COMPLETE",
        }
        _write_json(paths.open_event, completed_event)
        return acceptance
    except BaseException as exc:
        failed_event = {
            **event,
            "failed_at_utc": utc_now(),
            "failure": f"{type(exc).__name__}: {exc}",
            "state": "OPENED_FAILED",
        }
        _write_json(paths.open_event, failed_event)
        raise


def validate_conditional_result(
    *,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    paths = conditional_paths(output_root)
    issues: list[str] = []
    for path in (paths.acceptance_json, paths.run_manifest_json):
        if not path.is_file():
            issues.append(f"missing conditional artifact {path}")
    if issues:
        result = {"acceptance": "FAIL", "issues": issues}
        _write_json(paths.validation_json, result)
        return result
    acceptance = _load_json(paths.acceptance_json)
    manifest = _load_json(paths.run_manifest_json)
    if manifest.get("protocol_definition_sha256") != file_sha256(protocol_path):
        issues.append("conditional protocol hash mismatch")
    for metadata in manifest.get("files", {}).values():
        path = Path(str(metadata.get("path") or ""))
        if not path.is_file() or file_sha256(path) != metadata.get("sha256"):
            issues.append(f"conditional artifact hash mismatch {path}")
    if acceptance.get("lockbox_outcomes_accessed") is not True:
        issues.append("conditional acceptance does not record outcome access")
    if acceptance.get("stage8_v1_3_result_overridden") is not False:
        issues.append("conditional acceptance improperly overrides v1.3")
    result = {
        "acceptance": "PASS" if not issues else "FAIL",
        "conditional_promotion_status": acceptance.get(
            "conditional_promotion_status"
        ),
        "hard_gate_pass": acceptance.get("hard_gate_pass"),
        "lockbox_outcomes_accessed": True,
        "production_promotion_performed": False,
        "issues": issues,
    }
    _write_json(paths.validation_json, result)
    return result


def _write_source_shadow(
    *,
    dashboard_dir: Path,
    output_dir: Path,
    asof: str,
    conditional_acceptance_sha256: str,
) -> tuple[Path, Path, list[dict[str, str]]]:
    dashboard_manifest_path = (
        dashboard_dir / "machinery_final_rank_table_manifest.json"
    )
    dashboard_manifest = _load_json(dashboard_manifest_path)
    sidecar = Path(str(dashboard_manifest.get("sidecar") or ""))
    if not sidecar.is_file() or file_sha256(sidecar) != dashboard_manifest.get(
        "sidecar_sha256"
    ):
        raise ValueError("Latest machinery survivorship sidecar is invalid")
    rows = read_rows(sidecar)
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        raise ValueError("Conditional source shadow as-of mismatch")
    if any(
        str(row.get("portfolio_candidate_gate") or "") != "0"
        or str(row.get("oos_score_valid_flag") or "") != "0"
        for row in rows
    ):
        raise ValueError("Conditional source sidecar is not fully shadow-only")
    output_dir.mkdir(parents=True, exist_ok=True)
    rank_path = output_dir / "machinery_final_rank_table.csv"
    manifest_path = output_dir / "machinery_final_rank_table_manifest.json"
    write_rank_rows(rank_path, rows)
    manifest = {
        "acceptance": "PASS",
        "artifact_family": "machinery_conditional_source_shadow",
        "asof_date": asof,
        "conditional_acceptance_sha256": conditional_acceptance_sha256,
        "production_promoted": False,
        "rank_table_sha256": file_sha256(rank_path),
        "row_count": len(rows),
        "source_dashboard_manifest": str(dashboard_manifest_path),
        "source_dashboard_manifest_sha256": file_sha256(
            dashboard_manifest_path
        ),
        "source_sidecar": str(sidecar),
        "source_sidecar_sha256": file_sha256(sidecar),
    }
    _write_json(manifest_path, manifest)
    return rank_path, manifest_path, rows


def build_conditional_stage12_candidate(
    config: dict[str, Any],
    *,
    config_path: Path = DEFAULT_CONFIG_PATH,
    asof: str,
    protocol_path: Path = DEFAULT_PROTOCOL_PATH,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    protocol = load_conditional_protocol(protocol_path)
    paths = conditional_paths(output_root)
    validation = validate_conditional_result(
        protocol_path=protocol_path,
        output_root=output_root,
    )
    if validation.get("acceptance") != "PASS":
        raise ValueError("Conditional lockbox result is invalid")
    acceptance = _load_json(paths.acceptance_json)
    if (
        acceptance.get("conditional_promotion_status")
        != "READY_FOR_PORTFOLIO_SMOKE"
    ):
        raise ValueError("Conditional hard gates did not pass")
    stage12_paths = Stage12Paths(paths.stage12_root)
    if stage12_paths.lock_json.exists():
        current = validate_stage12_lock(output_root=paths.stage12_root)
        if current.get("acceptance") == "PASS":
            return _load_json(stage12_paths.lock_json)
        raise FileExistsError("Existing conditional Stage 12 candidate is invalid")
    portfolio_path = resolve_path(
        cfg_get(config, "machinery_stage12.portfolio_config_path"),
        base_dir=config_path.parent,
    )
    portfolio = load_yaml(portfolio_path)
    family = _portfolio_family(portfolio)
    cap = float(
        cfg_get(portfolio, "optimizer.sector_weight_caps.machinery", -1.0)
    )
    approved_cap = float(
        protocol["portfolio_contract"]["maximum_provisional_cap"]
    )
    if family.get("required") is not True or cap != approved_cap:
        raise ValueError("Conditional replacement cannot alter the live machinery cap")
    fixed_equal = {
        str(value)
        for value in cfg_get(
            portfolio,
            "optimizer.fixed_equal_weight_sleeves",
            [],
        )
    }
    if MODEL_FAMILY not in fixed_equal:
        raise ValueError("Portfolio layer no longer preserves machinery equal weights")
    active = _active_evidence(config, config_path=config_path)
    dashboard_root = resolve_path(
        cfg_get(config, "machinery_scoring.dashboard_root"),
        base_dir=config_path.parent,
    )
    source_rank, source_manifest, source_rows = _write_source_shadow(
        dashboard_dir=dashboard_root / asof,
        output_dir=paths.stage12_root / "source_shadow" / asof,
        asof=asof,
        conditional_acceptance_sha256=file_sha256(paths.acceptance_json),
    )
    frozen = _load_json(paths.freeze_manifest)
    candidate_weights = _float_map(
        frozen["candidate_weights"],
        name="conditional candidate weights",
    )
    spec = StrategySpec(
        name="long_only_q20_equal",
        portfolio_type="long_only",
        weighting="equal",
        quantile=0.20,
    )
    preview = production_preview_rows(
        source_rows,
        weights=candidate_weights,
        asof=asof,
        lock_date=str(protocol["evidence_window"]["sealed_start_date"]),
        score_model_version="machinery_oos_v1.4.0_conditional",
        model_version="machinery_oos_2026_03_conditional",
        scoring_contract_version=(
            "industrial_family_final_rank_table_v3_production"
        ),
        selection_spec=spec,
        minimum_positions=int(
            protocol["evaluation_contract"]["minimum_positions"]
        ),
        universe_policy=str(
            protocol["evaluation_contract"]["production_universe_policy"]
        ),
    )
    parity_rows = read_rows(paths.parity_csv)
    if not parity_rows or any(
        row.get("parity_status") != "PASS" for row in parity_rows
    ):
        raise ValueError("Conditional production policy parity did not pass")
    selection_policy = {
        "version": PRODUCTION_SELECTION_POLICY_VERSION,
        "variant": spec.name,
        "portfolio_type": spec.portfolio_type,
        "weighting": spec.weighting,
        "quantile": spec.quantile,
        "minimum_positions": int(
            protocol["evaluation_contract"]["minimum_positions"]
        ),
        "universe_policy": str(
            protocol["evaluation_contract"]["production_universe_policy"]
        ),
        "parity_period_count": len(parity_rows),
        "parity_status": "PASS",
        "conditional_status": protocol["conditional_status_label"],
    }
    issues = _validate_preview_rows(
        preview,
        asof=asof,
        selection_policy=selection_policy,
    )
    if issues:
        raise ValueError("Conditional production preview failed: " + ";".join(issues))
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
        raise ValueError("Portfolio adapter changed conditional membership")
    panel_manifest = stage8_paths(paths.panel_root).panel_manifest_json
    active_state = Path(str(active["activation_state_path"]))
    lock = {
        "acceptance": "PASS",
        "activation_mode": ACTIVATION_MODE_REPLACE_ACTIVE,
        "activation_requires_explicit_operator_approval": True,
        "active_activation_state": str(active_state),
        "adapter_investable_count": len(adapted),
        "broad_portfolio_universe_eligible_count": len(sleeve_rows),
        "conditional_acceptance": str(paths.acceptance_json),
        "conditional_acceptance_sha256": file_sha256(paths.acceptance_json),
        "conditional_protocol_sha256": file_sha256(protocol_path),
        "created_at_utc": utc_now(),
        "current_portfolio_cap": cap,
        "development_end_date": "2025-12-31",
        "live_dashboard_modified": False,
        "lockbox_start_date": str(
            protocol["evidence_window"]["sealed_start_date"]
        ),
        "machinery_portfolio_policy_sha256": (
            machinery_portfolio_policy_fingerprint(portfolio)
        ),
        "minimum_capacity_multiple": float(
            protocol["hard_gates"]["minimum_capacity_multiple"]
        ),
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
            PROJECT_ROOT / "portfolio_layer" / "optimizer" / "optimizer_core.py"
        ),
        "portfolio_optimizer_runner_sha256": file_sha256(
            PROJECT_ROOT
            / "portfolio_layer"
            / "optimizer"
            / "09_run_portfolio_optimizer.py"
        ),
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
        "recommended_model": "v14_equal_components_conditional",
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
        "target_aum_usd": float(
            protocol["portfolio_contract"]["target_aum_usd"]
        ),
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
            "artifact_family": "machinery_conditional_stage12_candidate",
            "created_at_utc": utc_now(),
            "production_promotion_performed": False,
            "files": {
                path.name: {"path": str(path), "sha256": file_sha256(path)}
                for path in artifacts
            },
        },
    )
    stage12_validation = validate_stage12_lock(
        output_root=paths.stage12_root
    )
    if stage12_validation.get("acceptance") != "PASS":
        raise ValueError("Conditional Stage 12 candidate failed validation")
    return lock
