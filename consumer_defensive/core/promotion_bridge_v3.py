"""Database-free bridge from sealed calibration-v2 evidence to promotion-v3."""

from __future__ import annotations

import hashlib
import math
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from consumer_defensive.core.calibration_execution_v2 import (
    FOLD_REGISTRY_SCHEMA,
    INPUT_MANIFEST_SCHEMA,
    PATH_ATTESTATION_SCHEMA,
    RESULTS_SCHEMA,
    VALIDATION_SCHEMA,
)
from consumer_defensive.core.calibration_preregistration_v2 import (
    validate_candidate_registry,
    validate_preregistration,
)
from consumer_defensive.core.capital_context_v1 import (
    validate_portfolio_capital_context,
)
from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    build_capital_allocation_context,
    build_production_model_contract,
    validate_capital_allocation_context,
    value_sha256,
)
from consumer_defensive.core.promotion_framework_v2 import (
    DECISION_SCHEMA,
    framework_sha256 as framework_sha256_v2,
    validate_calibration_decision,
    validate_framework as validate_framework_v2,
)
from consumer_defensive.core.promotion_input_v3 import (
    BENCHMARK_ATTESTATION_SCHEMA,
    build_promotion_input,
    validate_benchmark_attestation,
)


INPUT_BUILD_ATTESTATION_SCHEMA = "consumer_defensive_promotion_input_build_attestation_v3"
DESIGN_EVIDENCE_ROLE = "design_evidence"
DESIGN_EVIDENCE_MAXIMUM_STATE = "active_full"
SAFETY_ATTESTATION_NAMES = frozenset({
    "independent_validation_passed", "source_hashes_verified", "outer_oos_only",
    "no_lookahead", "chronology_complete", "returns_net_of_costs",
    "matched_daily_benchmark", "corporate_actions_reconciled",
    "terminal_events_reconciled", "production_model_contract_bound",
})
SOURCE_ARTIFACT_NAMES = frozenset({
    "candidate_registry", "preregistration", "input_manifest", "fold_registry",
    "realized_path_attestation", "matched_benchmark_attestation", "results",
    "decision", "independent_validation", "promotion_framework_v2",
    "promotion_framework_v3",
})
BRIDGE_METHODOLOGY_PATHS = (
    "consumer_defensive/core/calibration_execution_v2.py",
    "consumer_defensive/core/calibration_preregistration_v2.py",
    "consumer_defensive/core/calibration_v2.py",
    "consumer_defensive/core/promotion_artifacts_v3.py",
    "consumer_defensive/core/promotion_bridge_v3.py",
    "consumer_defensive/core/promotion_engine_v3.py",
    "consumer_defensive/core/promotion_framework_v2.py",
    "consumer_defensive/core/promotion_input_v3.py",
    "consumer_defensive/scripts/29_run_consumer_defensive_calibration_v2.py",
    "consumer_defensive/scripts/29a_build_consumer_defensive_promotion_input_v3.py",
    "consumer_defensive/data/consumer_defensive_promotion_framework_v3.yaml",
)


def file_sha256(path: Path) -> str:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise ValueError(f"source file is missing or unsafe: {resolved}")
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def methodology_file_sha256s(repository_root: Path) -> dict[str, str]:
    root = repository_root.expanduser().resolve()
    return {relative: file_sha256(root / relative) for relative in BRIDGE_METHODOLOGY_PATHS}


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _sequence(value: Any, *, label: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be an array")
    return list(value)


def _canonical_date(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _digest(value: Any, *, label: str) -> str:
    if (not isinstance(value, str) or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _self_hash(payload: Mapping[str, Any]) -> str:
    return value_sha256({key: value for key, value in payload.items() if key != "payload_sha256"})


def _sealed(value: Any, *, label: str, schema: str, asof_date: str) -> dict[str, Any]:
    payload = _mapping(value, label=label)
    if payload.get("schema_version") != schema or payload.get("model_family") != "consumer_defensive":
        raise ValueError(f"{label} has an unsupported schema/scope")
    if _canonical_date(payload.get("asof_date"), label=f"{label}.asof_date") != asof_date:
        raise ValueError(f"{label} as-of date does not match the package")
    if _self_hash(payload) != _digest(payload.get("payload_sha256"), label=f"{label}.payload_sha256"):
        raise ValueError(f"{label} self-hash mismatch")
    return payload


def _validate_decision_bindings(
    decision: Mapping[str, Any], bindings: Mapping[str, str]
) -> None:
    """Reconcile the v2 decision schema with the calibration evidence package.

    The v2 decision deliberately calls the sealed input-manifest digest
    ``input_panel_sha256``. Other calibration artifacts call the same digest
    ``input_manifest_sha256``. Keep that schema translation explicit so the
    bridge cannot silently accept a missing field or compare the wrong alias.
    """
    expected_by_decision_field = {
        "candidate_registry_sha256": bindings["candidate_registry_sha256"],
        "input_panel_sha256": bindings["input_manifest_sha256"],
        "fold_registry_sha256": bindings["fold_registry_sha256"],
        "code_sha256": bindings["code_sha256"],
    }
    for field, expected in expected_by_decision_field.items():
        if decision.get(field) != expected:
            raise ValueError(f"decision.{field} does not reconcile")


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric evidence")
    return parsed


def validate_capital_context_binding(
    *,
    portfolio_capital_context: Mapping[str, Any],
    capital_context_file_sha256: str,
    trusted_capital_context_file_sha256: str,
    evidence_asof_date: str,
    calibration_reference_notional_usd: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the allocation-only context and its external trust binding.

    The context is deliberately separate from predictive evidence. Its file
    digest is supplied by the caller and must match an independently trusted
    digest; its self-hash and allocation arithmetic are owned by the promotion
    engine validator. The historical calibration reference is reconciled
    explicitly so a capital-context change cannot silently reinterpret the
    sealed OOS liquidity ratios.
    """

    observed_file_sha = _digest(
        capital_context_file_sha256,
        label="capital_context_file_sha256",
    )
    trusted_file_sha = _digest(
        trusted_capital_context_file_sha256,
        label="trusted_capital_context_file_sha256",
    )
    if observed_file_sha != trusted_file_sha:
        raise ValueError("capital context file SHA-256 does not match the trusted digest")
    raw_context = validate_portfolio_capital_context(portfolio_capital_context)
    evidence_asof = _canonical_date(
        evidence_asof_date,
        label="capital context evidence_asof_date",
    )
    reference_notional = _finite(
        calibration_reference_notional_usd,
        label="calibration reference gross notional",
    )
    if reference_notional <= 0.0:
        raise ValueError("calibration reference gross notional must be positive")
    normalized = build_capital_allocation_context(
        asof_date=str(raw_context["asof_date"]),
        account_aum_usd=float(raw_context["account_aum_usd"]),
        active_sector_count=int(raw_context["active_sector_count"]),
        sector_max_fraction=float(raw_context["sector_cap_fraction"]),
        calibration_reference_notional_usd=reference_notional,
    )
    validated = validate_capital_allocation_context(
        normalized,
        evidence_asof_date=evidence_asof,
    )
    if not math.isclose(
        float(validated["sector_max_notional_usd"]),
        float(raw_context["sector_cap_notional_usd"]),
        rel_tol=0.0,
        abs_tol=1e-8,
    ):
        raise ValueError(
            "normalized Consumer sector budget does not reconcile to Portfolio context"
        )
    return raw_context, validated


def _outer_oos_from_path(*, cohort: str, horizon: int, result: Mapping[str, Any],
                         detail: Mapping[str, Any], path_rows: Sequence[Mapping[str, Any]],
                         asof_date: str) -> list[dict[str, str]]:
    label = f"{cohort}/{horizon}"
    rows = [dict(row) for row in path_rows]
    if not rows or value_sha256(rows) != detail.get("realized_path_attestation_sha256"):
        raise ValueError(f"{label}: realized path is empty or has the wrong detail hash")
    completion = _mapping(detail.get("completion_by_signal_date"), label=f"{label}.completion")
    folds = [_mapping(row, label=f"{label}.fold") for row in _sequence(detail.get("folds"), label=f"{label}.folds")]
    selected = _mapping(detail.get("selected_candidate_by_fold"), label=f"{label}.selected")
    fold_by_signal: dict[str, str] = {}
    fold_ids: list[str] = []
    prior_end = ""
    for fold in folds:
        fold_id = str(fold.get("fold_id") or "")
        test_dates = [_canonical_date(item, label=f"{label}.test_date") for item in _sequence(fold.get("test_dates"), label=f"{label}.test_dates")]
        if not fold_id or not test_dates or test_dates != sorted(set(test_dates)) or (prior_end and test_dates[0] <= prior_end):
            raise ValueError(f"{label}: outer fold chronology is invalid")
        prior_end = test_dates[-1]
        fold_ids.append(fold_id)
        for signal in test_dates:
            if signal in fold_by_signal:
                raise ValueError(f"{label}: signal belongs to multiple outer folds")
            fold_by_signal[signal] = fold_id
    if set(fold_ids) != set(selected):
        raise ValueError(f"{label}: selected candidates do not cover the fold census")

    realized_payload: list[dict[str, Any]] = []
    observations: dict[str, dict[str, str]] = {}
    prior_return = ""
    for row in rows:
        if row.get("cohort") != cohort or int(row.get("horizon_sessions") or 0) != horizon:
            raise ValueError(f"{label}: realized path scope mismatch")
        observation_id = str(row.get("observation_id") or "")
        source_id = str(row.get("source_portfolio_observation_id") or "")
        fold_id = str(row.get("fold_id") or "")
        signal = _canonical_date(row.get("signal_date"), label=f"{label}.signal")
        return_date = _canonical_date(row.get("return_date"), label=f"{label}.return")
        if not observation_id or not source_id or return_date <= signal or (prior_return and return_date <= prior_return):
            raise ValueError(f"{label}: realized path identity/chronology is invalid")
        prior_return = return_date
        if fold_by_signal.get(signal) != fold_id:
            raise ValueError(f"{label}: realized row has the wrong outer fold")
        completed = _canonical_date(completion.get(signal), label=f"{label}.completion")
        if not signal < completed <= asof_date:
            raise ValueError(f"{label}: outer label chronology is invalid")
        gross = _finite(row.get("gross_return"), label=f"{label}.gross_return")
        cost = _finite(row.get("transaction_cost"), label=f"{label}.transaction_cost")
        net = _finite(row.get("net_return"), label=f"{label}.net_return")
        if cost < 0.0 or not math.isclose(gross - cost, net, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"{label}: return is not net of transaction cost")
        observed = {"observation_id": source_id, "fold_id": fold_id,
                    "signal_date": signal, "label_completion_date": completed}
        if observations.setdefault(source_id, observed) != observed:
            raise ValueError(f"{label}: source identity changes within its path")
        realized_payload.append({"observation_id": observation_id,
            "source_portfolio_observation_id": source_id, "fold_id": fold_id,
            "return_date": return_date, "net_strategy_return": net})
    outer = sorted(observations.values(), key=lambda row: (row["signal_date"], row["observation_id"]))
    evidence = _mapping(result.get("evidence"), label=f"{label}.evidence")
    performance = _mapping(result.get("performance"), label=f"{label}.performance")
    v2_outer = [{"observation_id": row["observation_id"], "fold_id": row["fold_id"],
                 "asof_date": row["signal_date"], "label_completion_date": row["label_completion_date"]}
                for row in outer]
    expected = {"evaluation_role": "outer_test", "horizon_sessions": horizon,
        "observation_count": len(outer), "observation_ids_sha256": value_sha256({"value": v2_outer}),
        "fold_ids_sha256": value_sha256({"value": folds}),
        "signal_start_date": outer[0]["signal_date"], "signal_end_date": outer[-1]["signal_date"],
        "latest_label_completion_date": max(row["label_completion_date"] for row in outer),
        "realized_return_count": len(rows), "realized_return_stream_sha256": value_sha256({"value": realized_payload}),
        "realized_return_start_date": rows[0]["return_date"], "realized_return_end_date": rows[-1]["return_date"],
        "candidate_matrix_sha256": detail.get("candidate_matrix_sha256")}
    for field, expected_value in expected.items():
        if evidence.get(field) != expected_value:
            raise ValueError(f"{label}: result evidence {field} does not reconcile")
    if int(detail.get("outer_observation_count") or -1) != len(outer) or int(detail.get("realized_daily_return_count") or -1) != len(rows) or int(performance.get("paired_observation_count") or -1) != len(outer):
        raise ValueError(f"{label}: observation counts do not reconcile")
    return outer


def _matched_paths_and_cross_check(*, benchmark: Mapping[str, Any],
                                   strategy: Mapping[str, Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    benchmark_cohorts = _mapping(benchmark.get("cohorts"), label="benchmark.cohorts")
    strategy_cohorts = _mapping(strategy.get("cohorts"), label="strategy.cohorts")
    matched: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        benchmark_horizons = _mapping(benchmark_cohorts.get(cohort), label=f"benchmark.{cohort}")
        strategy_horizons = _mapping(strategy_cohorts.get(cohort), label=f"strategy.{cohort}")
        matched[cohort] = {}
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            benchmark_rows = [_mapping(row, label=f"benchmark.{cohort}.{key}") for row in _sequence(benchmark_horizons.get(key), label=f"benchmark.{cohort}.{key}")]
            strategy_rows = [_mapping(row, label=f"strategy.{cohort}.{key}") for row in _sequence(strategy_horizons.get(key), label=f"strategy.{cohort}.{key}")]
            if len(benchmark_rows) != len(strategy_rows):
                raise ValueError(f"{cohort}/{key}: benchmark/strategy row count mismatch")
            output: list[dict[str, Any]] = []
            for position, (benchmark_row, strategy_row) in enumerate(zip(benchmark_rows, strategy_rows)):
                expected = {"signal_date": strategy_row.get("signal_date"),
                    "return_date": strategy_row.get("return_date"),
                    "strategy_observation_id": strategy_row.get("observation_id"),
                    "strategy_net_return": strategy_row.get("net_return")}
                if any(benchmark_row.get(field) != value for field, value in expected.items()):
                    raise ValueError(f"{cohort}/{key}/{position}: benchmark is not bound to strategy path")
                output.append({"date": benchmark_row["return_date"],
                    "strategy_net_return": benchmark_row["strategy_net_return"],
                    "primary_benchmark_return": benchmark_row["primary_benchmark_return"],
                    "xlp_return": benchmark_row["xlp_return"], "spy_return": benchmark_row["spy_return"]})
            matched[cohort][key] = output
    validate_benchmark_attestation(benchmark, matched_paths_by_cohort=matched)
    return matched


def _validate_source_package(*, artifacts: Mapping[str, Mapping[str, Any]],
                             calibration_framework: Mapping[str, Any]) -> tuple[Any, ...]:
    required = SOURCE_ARTIFACT_NAMES - {"promotion_framework_v2", "promotion_framework_v3"}
    if set(artifacts) != required:
        raise ValueError("calibration artifact census is incomplete or unexpected")
    registry = validate_candidate_registry(artifacts["candidate_registry"])
    prereg = validate_preregistration(artifacts["preregistration"], candidate_registry=registry)
    asof_date = _canonical_date(prereg["asof_date"], label="asof_date")
    framework_v2 = validate_framework_v2(calibration_framework)
    if framework_sha256_v2(framework_v2) != prereg["framework_sha256"] or registry["framework_sha256"] != prereg["framework_sha256"]:
        raise ValueError("calibration framework/registration binding changed")
    input_manifest = _sealed(artifacts["input_manifest"], label="input_manifest", schema=INPUT_MANIFEST_SCHEMA, asof_date=asof_date)
    fold_registry = _sealed(artifacts["fold_registry"], label="fold_registry", schema=FOLD_REGISTRY_SCHEMA, asof_date=asof_date)
    path = _sealed(artifacts["realized_path_attestation"], label="realized_path_attestation", schema=PATH_ATTESTATION_SCHEMA, asof_date=asof_date)
    results = _sealed(artifacts["results"], label="results", schema=RESULTS_SCHEMA, asof_date=asof_date)
    decision = _sealed(artifacts["decision"], label="decision", schema=DECISION_SCHEMA, asof_date=asof_date)
    validate_calibration_decision(decision, framework=framework_v2)
    validation = _sealed(artifacts["independent_validation"], label="independent_validation", schema=VALIDATION_SCHEMA, asof_date=asof_date)
    benchmark = _mapping(artifacts["matched_benchmark_attestation"], label="matched benchmark")
    if benchmark.get("schema_version") != BENCHMARK_ATTESTATION_SCHEMA or benchmark.get("model_family") != "consumer_defensive":
        raise ValueError("matched benchmark attestation schema/scope changed")
    benchmark_sha = _digest(benchmark.get("payload_sha256"), label="benchmark.payload_sha256")
    if _self_hash(benchmark) != benchmark_sha:
        raise ValueError("matched benchmark attestation self-hash mismatch")

    if input_manifest.get("preregistration_sha256") != prereg["payload_sha256"] or fold_registry.get("preregistration_sha256") != prereg["payload_sha256"] or path.get("preregistration_sha256") != prereg["payload_sha256"] or results.get("preregistration_sha256") != prereg["payload_sha256"]:
        raise ValueError("calibration evidence is bound to a different preregistration")
    bindings = {
        "candidate_registry_sha256": registry["payload_sha256"],
        "input_manifest_sha256": input_manifest["payload_sha256"],
        "fold_registry_sha256": fold_registry["payload_sha256"],
        "realized_path_attestation_sha256": path["payload_sha256"],
        "matched_benchmark_attestation_sha256": benchmark_sha,
        "decision_payload_sha256": decision["payload_sha256"],
        "code_sha256": prereg["code_sha256"],
    }
    result_bindings = {
        field: expected for field, expected in bindings.items()
        if field != "code_sha256"
    }
    for label, payload, expected_bindings in (
        ("results", results, result_bindings),
        ("independent_validation", validation, bindings),
    ):
        for field, expected in expected_bindings.items():
            if payload.get(field) != expected:
                raise ValueError(f"{label}.{field} does not reconcile")
    _validate_decision_bindings(decision, bindings)
    if fold_registry.get("realized_path_attestation_sha256") != path["payload_sha256"]:
        raise ValueError("fold registry/path attestation hash mismatch")
    if fold_registry.get("matched_benchmark_attestation_sha256") != benchmark_sha:
        raise ValueError("fold registry/benchmark attestation hash mismatch")
    if results.get("matched_benchmark_attestation_sha256") != benchmark_sha or validation.get("matched_benchmark_attestation_sha256") != benchmark_sha:
        raise ValueError("matched benchmark is detached from sealed calibration evidence")
    if results.get("production_promotion_enabled") is not False or results.get("portfolio_write_enabled") is not False:
        raise ValueError("calibration results must remain report-only")
    if validation.get("status") != "PASS" or validation.get("production_write_performed") is not False or validation.get("portfolio_write_performed") is not False:
        raise ValueError("independent validation is not a clean report-only PASS")
    if validation.get("framework_sha256") != framework_sha256_v2(framework_v2):
        raise ValueError("independent validation/calibration framework mismatch")
    if prereg.get("registered_before_label_evaluation") is not True or prereg.get("forward_label_accessed") is not False:
        raise ValueError("calibration preregistration does not prove no-lookahead")
    terminal = _mapping(input_manifest.get("terminal_event_validation"), label="terminal validation")
    if terminal.get("status") != "PASS" or terminal.get("errors") not in (None, []):
        raise ValueError("terminal-event evidence is not reconciled")
    mark_policy = _mapping(input_manifest.get("realized_price_mark_policy"), label="price mark policy")
    if mark_policy.get("entry_requires_observed_original_bar") is not True or mark_policy.get("pre_listing_carry") is not False or mark_policy.get("unclassified_terminal_carry") is not False:
        raise ValueError("realized-price safety policy changed")

    result_cohorts = _mapping(results.get("cohort_horizon_results"), label="results.cohorts")
    fold_cohorts = _mapping(fold_registry.get("cohorts"), label="folds.cohorts")
    path_cohorts = _mapping(path.get("cohorts"), label="path.cohorts")
    if set(result_cohorts) != REQUIRED_COHORTS or set(fold_cohorts) != REQUIRED_COHORTS or set(path_cohorts) != REQUIRED_COHORTS:
        raise ValueError("calibration cohort census changed")
    performance: dict[str, dict[str, Mapping[str, Any]]] = {}
    outer: dict[str, dict[str, list[dict[str, str]]]] = {}
    contracts: dict[str, Mapping[str, Any]] = {}
    candidates = [dict(row) for row in registry["candidates"]]
    expected_horizons = {str(value) for value in REQUIRED_HORIZONS}

    for cohort in sorted(REQUIRED_COHORTS):
        result_horizons = _mapping(result_cohorts[cohort], label=f"results.{cohort}")
        fold_horizons = _mapping(fold_cohorts[cohort], label=f"folds.{cohort}")
        path_horizons = _mapping(path_cohorts[cohort], label=f"path.{cohort}")
        if set(result_horizons) != expected_horizons or set(fold_horizons) != expected_horizons or set(path_horizons) != expected_horizons:
            raise ValueError(f"{cohort}: calibration horizon census changed")
        performance[cohort], outer[cohort] = {}, {}
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            result = _mapping(result_horizons[key], label=f"results.{cohort}.{key}")
            detail = _mapping(fold_horizons[key], label=f"folds.{cohort}.{key}")
            rows = [_mapping(row, label=f"path.{cohort}.{key}") for row in _sequence(path_horizons[key], label=f"path.{cohort}.{key}")]
            performance[cohort][key] = _mapping(result.get("performance"), label=f"performance.{cohort}.{key}")
            outer[cohort][key] = _outer_oos_from_path(cohort=cohort, horizon=horizon,
                result=result, detail=detail, path_rows=rows, asof_date=asof_date)
        champion = _mapping(fold_horizons["63"], label=f"{cohort}.champion")
        folds = [_mapping(row, label=f"{cohort}.champion_fold") for row in _sequence(champion.get("folds"), label=f"{cohort}.folds")]
        latest = max(folds, key=lambda row: max(str(value) for value in row["test_dates"]))
        selected = _mapping(champion.get("selected_candidate_by_fold"), label=f"{cohort}.selected")
        selected_id = str(selected.get(str(latest["fold_id"])) or "")
        matches = [row for row in candidates if row.get("candidate_id") == selected_id
                   and row.get("cohort") == cohort and int(row.get("horizon_sessions") or 0) == 63]
        if len(matches) != 1:
            raise ValueError(f"{cohort}: latest 63-session winner is not uniquely registered")
        candidate = matches[0]
        contracts[cohort] = build_production_model_contract(cohort=cohort,
            selected_candidate_id=selected_id, candidate_definition=candidate,
            candidate_registry_sha256=registry["payload_sha256"],
            score_model_version=f"consumer_defensive_calibration_v2:{selected_id}",
            scoring_contract_version=str(candidate["scoring_policy_id"]))
    matched = _matched_paths_and_cross_check(benchmark=benchmark, strategy=path)
    return asof_date, performance, outer, contracts, matched


def build_input_build_attestation(*, asof_date: str, source_file_sha256s: Mapping[str, str],
        source_payload_sha256s: Mapping[str, str], source_calibration_code_sha256: str,
        bridge_methodology_file_sha256s: Mapping[str, str],
        production_model_contracts: Mapping[str, Mapping[str, Any]],
        benchmark_attestation_sha256: str, promotion_input_sha256: str,
        capital_context_asof_date: str, capital_context_file_sha256: str,
        trusted_capital_context_file_sha256: str,
        capital_context_payload_sha256: str,
        normalized_capital_context_payload_sha256: str) -> dict[str, Any]:
    if set(source_file_sha256s) != SOURCE_ARTIFACT_NAMES:
        raise ValueError("input-build source-file census changed")
    checks = {name: True for name in SAFETY_ATTESTATION_NAMES}
    payload: dict[str, Any] = {
        "schema_version": INPUT_BUILD_ATTESTATION_SCHEMA,
        "model_family": "consumer_defensive",
        "asof_date": _canonical_date(asof_date, label="asof_date"),
        "evidence_role": DESIGN_EVIDENCE_ROLE,
        "maximum_authorized_state": DESIGN_EVIDENCE_MAXIMUM_STATE,
        "source_file_sha256s": {key: _digest(value, label=f"source_file.{key}") for key, value in sorted(source_file_sha256s.items())},
        "source_payload_sha256s": {key: _digest(value, label=f"source_payload.{key}") for key, value in sorted(source_payload_sha256s.items())},
        "source_calibration_code_sha256": _digest(source_calibration_code_sha256, label="source_calibration_code_sha256"),
        "bridge_methodology_file_sha256s": {key: _digest(value, label=f"methodology.{key}") for key, value in sorted(bridge_methodology_file_sha256s.items())},
        "bridge_methodology_sha256": value_sha256(dict(sorted(bridge_methodology_file_sha256s.items()))),
        "production_model_contract_sha256s": {cohort: _digest(production_model_contracts[cohort]["payload_sha256"], label=f"contract.{cohort}") for cohort in sorted(REQUIRED_COHORTS)},
        "benchmark_attestation_sha256": _digest(benchmark_attestation_sha256, label="benchmark_attestation_sha256"),
        "promotion_input_sha256": _digest(promotion_input_sha256, label="promotion_input_sha256"),
        "capital_context_asof_date": _canonical_date(
            capital_context_asof_date,
            label="capital_context_asof_date",
        ),
        "capital_context_file_sha256": _digest(
            capital_context_file_sha256,
            label="capital_context_file_sha256",
        ),
        "trusted_capital_context_file_sha256": _digest(
            trusted_capital_context_file_sha256,
            label="trusted_capital_context_file_sha256",
        ),
        "capital_context_payload_sha256": _digest(
            capital_context_payload_sha256,
            label="capital_context_payload_sha256",
        ),
        "normalized_capital_context_payload_sha256": _digest(
            normalized_capital_context_payload_sha256,
            label="normalized_capital_context_payload_sha256",
        ),
        "capital_context_counts_as_fresh_predictive_evidence": False,
        "validation_checks": checks,
        "database_read_performed": False,
        "database_write_performed": False,
        "portfolio_write_performed": False,
    }
    payload["payload_sha256"] = _self_hash(payload)
    return validate_input_build_attestation(payload)


def validate_input_build_attestation(payload: Mapping[str, Any]) -> dict[str, Any]:
    required = {"schema_version", "model_family", "asof_date", "evidence_role",
        "maximum_authorized_state", "source_file_sha256s", "source_payload_sha256s",
        "source_calibration_code_sha256", "bridge_methodology_file_sha256s",
        "bridge_methodology_sha256", "production_model_contract_sha256s",
        "benchmark_attestation_sha256", "promotion_input_sha256",
        "capital_context_asof_date", "capital_context_file_sha256",
        "trusted_capital_context_file_sha256", "capital_context_payload_sha256",
        "normalized_capital_context_payload_sha256",
        "capital_context_counts_as_fresh_predictive_evidence", "validation_checks",
        "database_read_performed", "database_write_performed", "portfolio_write_performed",
        "payload_sha256"}
    item = _mapping(payload, label="input-build attestation")
    if set(item) != required:
        raise ValueError("input-build attestation schema changed")
    if item["schema_version"] != INPUT_BUILD_ATTESTATION_SCHEMA or item["model_family"] != "consumer_defensive" or item["evidence_role"] != DESIGN_EVIDENCE_ROLE or item["maximum_authorized_state"] != DESIGN_EVIDENCE_MAXIMUM_STATE:
        raise ValueError("input-build attestation policy changed")
    asof = _canonical_date(item["asof_date"], label="input-build asof_date")
    context_asof = _canonical_date(
        item["capital_context_asof_date"],
        label="capital_context_asof_date",
    )
    if context_asof < asof:
        raise ValueError("capital context predates the calibration evidence")
    source_files = _mapping(item["source_file_sha256s"], label="source files")
    if set(source_files) != SOURCE_ARTIFACT_NAMES:
        raise ValueError("input-build source-file census changed")
    if set(_mapping(item["source_payload_sha256s"], label="source payloads")) != (
        SOURCE_ARTIFACT_NAMES - {"promotion_framework_v2", "promotion_framework_v3"}
    ):
        raise ValueError("input-build source-payload census changed")
    for group_name in ("source_file_sha256s", "source_payload_sha256s",
                       "bridge_methodology_file_sha256s", "production_model_contract_sha256s"):
        for key, value in _mapping(item[group_name], label=group_name).items():
            _digest(value, label=f"{group_name}.{key}")
    if set(item["production_model_contract_sha256s"]) != REQUIRED_COHORTS:
        raise ValueError("model-contract cohort census changed")
    methodology = _mapping(item["bridge_methodology_file_sha256s"], label="methodology")
    if set(methodology) != set(BRIDGE_METHODOLOGY_PATHS) or value_sha256(dict(sorted(methodology.items()))) != item["bridge_methodology_sha256"]:
        raise ValueError("bridge methodology census/hash changed")
    for field in ("source_calibration_code_sha256", "bridge_methodology_sha256",
                  "benchmark_attestation_sha256", "promotion_input_sha256",
                  "capital_context_file_sha256",
                  "trusted_capital_context_file_sha256",
                  "capital_context_payload_sha256",
                  "normalized_capital_context_payload_sha256", "payload_sha256"):
        _digest(item[field], label=field)
    if item["capital_context_file_sha256"] != item["trusted_capital_context_file_sha256"]:
        raise ValueError("capital context file hash is not trusted")
    if item["capital_context_counts_as_fresh_predictive_evidence"] is not False:
        raise ValueError("capital context cannot authorize fresh predictive evidence")
    checks = _mapping(item["validation_checks"], label="validation_checks")
    if set(checks) != SAFETY_ATTESTATION_NAMES or any(value is not True for value in checks.values()):
        raise ValueError("input-build validation checks are incomplete")
    if item["database_read_performed"] is not False or item["database_write_performed"] is not False or item["portfolio_write_performed"] is not False:
        raise ValueError("promotion bridge must remain database/Portfolio free")
    if _self_hash(item) != item["payload_sha256"]:
        raise ValueError("input-build attestation self-hash mismatch")
    return item


def build_bridge_artifacts(*, artifacts: Mapping[str, Mapping[str, Any]],
        calibration_framework: Mapping[str, Any], promotion_framework: Mapping[str, Any],
        source_file_sha256s: Mapping[str, str],
        bridge_methodology_file_sha256s: Mapping[str, str],
        portfolio_capital_context: Mapping[str, Any],
        capital_context_file_sha256: str,
        trusted_capital_context_file_sha256: str) -> dict[str, Any]:
    asof_date, performance, outer, contracts, matched = _validate_source_package(
        artifacts=artifacts, calibration_framework=calibration_framework)
    preregistration_liquidity = _mapping(
        artifacts["preregistration"].get("liquidity_policy"),
        label="preregistration.liquidity_policy",
    )
    raw_capital_context, capital_context = validate_capital_context_binding(
        portfolio_capital_context=portfolio_capital_context,
        capital_context_file_sha256=capital_context_file_sha256,
        trusted_capital_context_file_sha256=trusted_capital_context_file_sha256,
        evidence_asof_date=asof_date,
        calibration_reference_notional_usd=preregistration_liquidity.get(
            "reference_gross_notional_usd"
        ),
    )
    benchmark = artifacts["matched_benchmark_attestation"]
    methodology_sha = value_sha256(dict(sorted(bridge_methodology_file_sha256s.items())))
    combined_code_sha = value_sha256({
        "bridge_methodology_sha256": methodology_sha,
        "source_calibration_code_sha256": artifacts["preregistration"]["code_sha256"],
    })
    source_lineage = {
        "source_decision_sha256": artifacts["decision"]["payload_sha256"],
        "source_results_sha256": artifacts["results"]["payload_sha256"],
        "input_panel_sha256": artifacts["input_manifest"]["payload_sha256"],
        "fold_registry_sha256": artifacts["fold_registry"]["payload_sha256"],
        "candidate_registry_sha256": artifacts["candidate_registry"]["payload_sha256"],
        "code_sha256": combined_code_sha,
        "benchmark_path_source_sha256": benchmark["payload_sha256"],
    }
    safety = {name: True for name in SAFETY_ATTESTATION_NAMES}
    promotion_input = build_promotion_input(asof_date=asof_date,
        evidence_role=DESIGN_EVIDENCE_ROLE, framework=promotion_framework,
        source_lineage=source_lineage, safety_attestations=safety,
        performance_by_cohort=performance, matched_paths_by_cohort=matched,
        outer_oos_observations_by_cohort=outer,
        production_model_contracts=contracts, benchmark_attestation=benchmark,
        capital_allocation_context=capital_context)
    source_payload_sha256s = {key: str(value["payload_sha256"])
                              for key, value in artifacts.items()}
    build_attestation = build_input_build_attestation(asof_date=asof_date,
        source_file_sha256s=source_file_sha256s,
        source_payload_sha256s=source_payload_sha256s,
        source_calibration_code_sha256=str(artifacts["preregistration"]["code_sha256"]),
        bridge_methodology_file_sha256s=bridge_methodology_file_sha256s,
        production_model_contracts=contracts,
        benchmark_attestation_sha256=str(benchmark["payload_sha256"]),
        promotion_input_sha256=str(promotion_input["payload_sha256"]),
        capital_context_asof_date=str(raw_capital_context["asof_date"]),
        capital_context_file_sha256=capital_context_file_sha256,
        trusted_capital_context_file_sha256=trusted_capital_context_file_sha256,
        capital_context_payload_sha256=str(raw_capital_context["payload_sha256"]),
        normalized_capital_context_payload_sha256=str(
            capital_context["payload_sha256"]
        ))
    return {"production_model_contracts": contracts,
            "benchmark_attestation": dict(benchmark),
            "portfolio_capital_context": raw_capital_context,
            "capital_allocation_context": capital_context,
            "promotion_input": promotion_input,
            "input_build_attestation": build_attestation}


__all__ = [
    "BRIDGE_METHODOLOGY_PATHS", "DESIGN_EVIDENCE_MAXIMUM_STATE",
    "DESIGN_EVIDENCE_ROLE", "INPUT_BUILD_ATTESTATION_SCHEMA",
    "SAFETY_ATTESTATION_NAMES", "SOURCE_ARTIFACT_NAMES", "build_bridge_artifacts",
    "build_input_build_attestation", "file_sha256",
    "methodology_file_sha256s", "validate_capital_context_binding",
    "validate_input_build_attestation",
]
