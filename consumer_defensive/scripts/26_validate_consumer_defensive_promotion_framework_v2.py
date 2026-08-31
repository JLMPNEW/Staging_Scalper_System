#!/usr/bin/env python3
"""Validate the v2 framework, decisions, and optional sealed calibration evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from consumer_defensive.core.promotion_framework_v2 import (  # noqa: E402
    REQUIRED_COHORTS,
    REQUIRED_HORIZONS,
    canonical_sha256,
    framework_sha256,
    load_framework,
    validate_calibration_decision,
)
from consumer_defensive.core.shared_services import (  # noqa: E402
    load_shared_service_contract,
    shared_service_contract_sha256,
)
from consumer_defensive.core.promotion_input_v3 import (  # noqa: E402
    validate_benchmark_attestation,
)


EVIDENCE_FILES = {
    "input_manifest": "consumer_defensive_calibration_input_manifest_v2.json",
    "fold_registry": "consumer_defensive_calibration_fold_registry_v2.json",
    "path_attestation": "consumer_defensive_calibration_realized_path_attestation_v2.json",
    "benchmark_attestation": "consumer_defensive_matched_benchmark_attestation_v3.json",
    "results": "consumer_defensive_calibration_results_v2.json",
    "decision": "consumer_defensive_calibration_decision_v2.json",
    "execution_validation": "consumer_defensive_calibration_independent_validation_v2.json",
}
EVIDENCE_SCHEMAS = {
    "input_manifest": "consumer_defensive_calibration_input_manifest_v2",
    "fold_registry": "consumer_defensive_calibration_fold_registry_v2",
    "path_attestation": "consumer_defensive_calibration_realized_path_attestation_v2",
    "benchmark_attestation": "consumer_defensive_matched_benchmark_attestation_v3",
    "results": "consumer_defensive_calibration_results_v2",
    "decision": "consumer_defensive_calibration_decision_v2",
    "execution_validation": "consumer_defensive_calibration_independent_validation_v2",
}
EXPECTED_PATH_POLICY = {
    "position_accounting": "buy_and_hold_sleeves_between_next_signal_rebalances",
    "rebalance_schedule": "next_selected_month_end_signal_plus_entry_lag",
    "final_block_sessions": 21,
    "absolute_metric_role": "daily_realized_monthly_rebalanced_absolute_profitability",
    "forward_label_role": "horizon_specific_forward_labels_and_relative_metrics",
    "overlapping_forward_label_reconciliation": "not_a_trade_pnl_identity",
    "terminal_accounting": "reviewed_economic_value_per_original_share",
}
PATH_ROOT_KEYS = frozenset(
    {
        "schema_version",
        "model_family",
        "asof_date",
        "preregistration_sha256",
        "path_policy",
        "cohorts",
        "payload_sha256",
    }
)
PATH_ROW_KEYS = frozenset(
    {
        "observation_id",
        "source_portfolio_observation_id",
        "fold_id",
        "cohort",
        "horizon_sessions",
        "signal_date",
        "entry_date",
        "return_date",
        "prior_nav",
        "current_nav",
        "entry_cash_value",
        "cash_value",
        "market_exposure_value",
        "gross_exposure_ratio",
        "gross_return",
        "transaction_cost",
        "net_return",
        "positions",
    }
)
POSITION_KEYS = frozenset(
    {
        "ticker",
        "units",
        "prior_mark",
        "current_mark",
        "prior_value",
        "current_value",
        "prior_provenance",
        "current_provenance",
        "prior_cash_component",
        "current_cash_component",
        "prior_market_component",
        "current_market_component",
        "terminal_event_sha256",
    }
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ABS_TOLERANCE = 1e-12


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--framework",
        type=Path,
        default=ROOT / "consumer_defensive/data/consumer_defensive_promotion_framework_v2.yaml",
    )
    parser.add_argument(
        "--shared-service-contract",
        type=Path,
        default=ROOT / "consumer_defensive/data/consumer_defensive_shared_service_contract_v1.yaml",
    )
    parser.add_argument("--decision", type=Path)
    parser.add_argument("--previous-decision", type=Path)
    parser.add_argument("--decision-history", type=Path, help="JSON array from genesis through predecessor")
    parser.add_argument(
        "--evidence-root",
        type=Path,
        help="Script 29 output directory containing the exact sealed evidence files",
    )
    return parser


def _duplicate_safe_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON object contains a duplicate key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"JSON contains a non-finite constant: {value}")


def _load_json(path: Path, *, label: str) -> Any:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is missing or unsafe: {resolved}")
    return json.loads(
        resolved.read_text(encoding="utf-8"),
        object_pairs_hook=_duplicate_safe_object,
        parse_constant=_reject_json_constant,
    )


def _mapping(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _exact_mapping(value: Any, keys: frozenset[str], *, label: str) -> dict[str, Any]:
    result = _mapping(value, label=label)
    if set(result) != keys:
        raise ValueError(f"{label} has an unexpected schema")
    return result


def _digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric data")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric data") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric data")
    return parsed


def _integer(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _iso_date(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return value


def _close(left: float, right: float, *, label: str) -> None:
    if not math.isclose(left, right, rel_tol=0.0, abs_tol=ABS_TOLERANCE):
        raise ValueError(f"realized path {label} identity failed: expected={left!r}, observed={right!r}")


def _validate_self_hash(payload: Mapping[str, Any], *, label: str) -> str:
    supplied = _digest(payload.get("payload_sha256"), label=f"{label}.payload_sha256")
    if supplied != canonical_sha256(payload):
        raise ValueError(f"{label} self-hash mismatch")
    return supplied


def _equal_binding(label: str, *values: Any) -> Any:
    if not values or any(value != values[0] for value in values[1:]):
        raise ValueError(f"evidence cross-hash binding failed: {label}")
    return values[0]


def _validate_position(raw: Any, *, row_label: str) -> dict[str, Any]:
    item = _exact_mapping(raw, POSITION_KEYS, label=f"{row_label}.position")
    ticker = str(item["ticker"] or "").strip().upper()
    if not ticker or ticker != item["ticker"]:
        raise ValueError(f"{row_label}: position ticker must be normalized and nonblank")
    units = _finite(item["units"], label=f"{row_label}.{ticker}.units")
    if units <= 0.0:
        raise ValueError(f"{row_label}.{ticker}: position units must be positive")
    numeric = {
        name: _finite(item[name], label=f"{row_label}.{ticker}.{name}")
        for name in (
            "prior_mark",
            "current_mark",
            "prior_value",
            "current_value",
            "prior_cash_component",
            "current_cash_component",
            "prior_market_component",
            "current_market_component",
        )
    }
    if any(value < 0.0 for value in numeric.values()):
        raise ValueError(f"{row_label}.{ticker}: long-only marks/components cannot be negative")
    if not str(item["prior_provenance"] or "").strip() or not str(
        item["current_provenance"] or ""
    ).strip():
        raise ValueError(f"{row_label}.{ticker}: price provenance must be nonblank")
    terminal_hash = str(item["terminal_event_sha256"] or "")
    if terminal_hash:
        _digest(terminal_hash, label=f"{row_label}.{ticker}.terminal_event_sha256")
    _close(numeric["prior_value"], units * numeric["prior_mark"], label="prior position value")
    _close(numeric["current_value"], units * numeric["current_mark"], label="current position value")
    _close(
        numeric["prior_mark"],
        numeric["prior_cash_component"] + numeric["prior_market_component"],
        label="prior cash/market mark",
    )
    _close(
        numeric["current_mark"],
        numeric["current_cash_component"] + numeric["current_market_component"],
        label="current cash/market mark",
    )
    return {**item, "ticker": ticker, "units": units, **numeric}


def _validate_path_row(
    raw: Any,
    *,
    cohort: str,
    horizon: int,
    row_index: int,
) -> dict[str, Any]:
    label = f"path.{cohort}.{horizon}[{row_index}]"
    row = _exact_mapping(raw, PATH_ROW_KEYS, label=label)
    if row["cohort"] != cohort or row["horizon_sessions"] != horizon:
        raise ValueError(f"{label}: containing cohort/horizon lineage mismatch")
    for key in ("observation_id", "source_portfolio_observation_id", "fold_id"):
        if not isinstance(row[key], str) or not row[key].strip():
            raise ValueError(f"{label}.{key} must be nonblank")
    signal = _iso_date(row["signal_date"], label=f"{label}.signal_date")
    entry = _iso_date(row["entry_date"], label=f"{label}.entry_date")
    returned = _iso_date(row["return_date"], label=f"{label}.return_date")
    if not signal < entry < returned:
        raise ValueError(f"{label}: signal/entry/return chronology is invalid")
    positions_raw = row["positions"]
    if not isinstance(positions_raw, list) or not positions_raw:
        raise ValueError(f"{label}: nonempty position detail is required")
    positions = [
        _validate_position(item, row_label=label) for item in positions_raw
    ]
    tickers = [item["ticker"] for item in positions]
    if tickers != sorted(tickers) or len(set(tickers)) != len(tickers):
        raise ValueError(f"{label}: positions must have unique sorted tickers")
    entry_cash = _finite(row["entry_cash_value"], label=f"{label}.entry_cash_value")
    if not 0.0 <= entry_cash <= 1.0:
        raise ValueError(f"{label}: entry cash must be in [0, 1]")
    prior_nav = entry_cash + sum(item["prior_value"] for item in positions)
    current_nav = entry_cash + sum(item["current_value"] for item in positions)
    if prior_nav <= 0.0 or current_nav <= 0.0:
        raise ValueError(f"{label}: NAV must remain positive")
    cash_value = entry_cash + sum(
        item["units"] * item["current_cash_component"] for item in positions
    )
    signed_market_value = sum(
        item["units"] * item["current_market_component"] for item in positions
    )
    market_exposure = sum(
        abs(item["units"] * item["current_market_component"]) for item in positions
    )
    expected_gross = current_nav / prior_nav - 1.0
    transaction_cost = _finite(row["transaction_cost"], label=f"{label}.transaction_cost")
    if not 0.0 <= transaction_cost < 1.0:
        raise ValueError(f"{label}: transaction cost must be in [0, 1)")
    _close(prior_nav, _finite(row["prior_nav"], label=f"{label}.prior_nav"), label="prior NAV")
    _close(current_nav, _finite(row["current_nav"], label=f"{label}.current_nav"), label="current NAV")
    _close(cash_value, _finite(row["cash_value"], label=f"{label}.cash_value"), label="cash value")
    _close(
        market_exposure,
        _finite(row["market_exposure_value"], label=f"{label}.market_exposure_value"),
        label="market exposure",
    )
    _close(current_nav, cash_value + signed_market_value, label="cash plus signed market NAV")
    _close(
        market_exposure / current_nav,
        _finite(row["gross_exposure_ratio"], label=f"{label}.gross_exposure_ratio"),
        label="gross exposure ratio",
    )
    _close(expected_gross, _finite(row["gross_return"], label=f"{label}.gross_return"), label="gross return")
    _close(
        expected_gross - transaction_cost,
        _finite(row["net_return"], label=f"{label}.net_return"),
        label="net return",
    )
    return {
        **row,
        "entry_cash_value": entry_cash,
        "prior_nav": prior_nav,
        "current_nav": current_nav,
        "transaction_cost": transaction_cost,
        "positions": positions,
    }


def _validate_path_continuity(rows: Sequence[dict[str, Any]], *, label: str) -> None:
    if [row["return_date"] for row in rows] != sorted(row["return_date"] for row in rows):
        raise ValueError(f"{label}: realized path rows must be chronological")
    if len({row["return_date"] for row in rows}) != len(rows):
        raise ValueError(f"{label}: realized return dates overlap")
    prior_by_source: dict[str, dict[str, Any]] = {}
    closed_sources: set[str] = set()
    previous_source: str | None = None
    for row in rows:
        source = row["source_portfolio_observation_id"]
        previous = prior_by_source.get(source)
        if source != previous_source and source in closed_sources:
            raise ValueError(f"{label}: a portfolio path block is noncontiguous")
        if previous_source is not None and source != previous_source:
            closed_sources.add(previous_source)
        if previous is None:
            _close(1.0, row["prior_nav"], label="entry unit NAV")
        else:
            for key in ("fold_id", "signal_date", "entry_date", "entry_cash_value"):
                if row[key] != previous[key]:
                    raise ValueError(f"{label}: portfolio-block {key} changed between sessions")
            _close(previous["current_nav"], row["prior_nav"], label="daily NAV continuity")
            if row["transaction_cost"] != 0.0:
                raise ValueError(f"{label}: transaction cost can occur only on the first path row")
            previous_positions = {item["ticker"]: item for item in previous["positions"]}
            positions = {item["ticker"]: item for item in row["positions"]}
            if set(positions) != set(previous_positions):
                raise ValueError(f"{label}: buy-and-hold ticker census changed inside a block")
            for ticker, item in positions.items():
                prior_item = previous_positions[ticker]
                _close(prior_item["units"], item["units"], label="buy-and-hold units")
                _close(prior_item["current_mark"], item["prior_mark"], label="daily mark continuity")
                _close(
                    prior_item["current_cash_component"],
                    item["prior_cash_component"],
                    label="daily cash-component continuity",
                )
                _close(
                    prior_item["current_market_component"],
                    item["prior_market_component"],
                    label="daily market-component continuity",
                )
        prior_by_source[source] = row
        previous_source = source


def _validate_realized_path(
    payload: Mapping[str, Any],
    *,
    fold_registry: Mapping[str, Any],
) -> int:
    root = _exact_mapping(payload, PATH_ROOT_KEYS, label="realized path attestation")
    if root["path_policy"] != EXPECTED_PATH_POLICY:
        raise ValueError("realized path policy is not the independently expected policy")
    cohorts = _mapping(root["cohorts"], label="realized path cohorts")
    if set(cohorts) != REQUIRED_COHORTS:
        raise ValueError("realized path must cover the exact cohort census")
    fold_cohorts = _mapping(fold_registry.get("cohorts"), label="fold registry cohorts")
    if set(fold_cohorts) != REQUIRED_COHORTS:
        raise ValueError("fold registry must cover the exact cohort census")
    observed_ids: set[str] = set()
    total = 0
    for cohort in sorted(REQUIRED_COHORTS):
        horizons = _mapping(cohorts[cohort], label=f"realized path.{cohort}")
        fold_horizons = _mapping(fold_cohorts[cohort], label=f"fold registry.{cohort}")
        expected_horizons = {str(value) for value in REQUIRED_HORIZONS}
        if set(horizons) != expected_horizons or set(fold_horizons) != expected_horizons:
            raise ValueError(f"{cohort}: exact 21/63/126 path and fold evidence is required")
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            raw_rows = horizons[key]
            if not isinstance(raw_rows, list) or not raw_rows:
                raise ValueError(f"realized path.{cohort}.{key} must be a nonempty list")
            rows = [
                _validate_path_row(item, cohort=cohort, horizon=horizon, row_index=index)
                for index, item in enumerate(raw_rows)
            ]
            for row in rows:
                identity = row["observation_id"]
                if identity in observed_ids:
                    raise ValueError("realized path observation identities must be globally unique")
                observed_ids.add(identity)
            _validate_path_continuity(rows, label=f"realized path.{cohort}.{key}")
            detail = _mapping(fold_horizons[key], label=f"fold registry.{cohort}.{key}")
            # Execution hashes a list directly rather than a top-level self-hashed
            # object. Reproduce that exact canonical JSON digest independently.
            list_hash = _canonical_value_sha256(raw_rows)
            _equal_binding(
                f"{cohort}.{key}.path row list",
                _digest(
                    detail.get("realized_path_attestation_sha256"),
                    label=f"fold registry.{cohort}.{key}.realized_path_attestation_sha256",
                ),
                list_hash,
            )
            if _integer(
                detail.get("realized_daily_return_count"),
                label=f"fold registry.{cohort}.{key}.realized_daily_return_count",
                minimum=1,
            ) != len(rows):
                raise ValueError(f"{cohort}.{key}: realized path row count mismatch")
            total += len(rows)
    return total


def _canonical_value_sha256(value: Any) -> str:
    import hashlib

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_evidence_root(root: Path) -> dict[str, dict[str, Any]]:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir() or resolved.is_symlink():
        raise FileNotFoundError(f"--evidence-root is missing or unsafe: {resolved}")
    payloads: dict[str, dict[str, Any]] = {}
    for label, filename in EVIDENCE_FILES.items():
        path = (resolved / filename).resolve()
        try:
            path.relative_to(resolved)
        except ValueError as exc:
            raise ValueError(f"evidence file escapes --evidence-root: {path}") from exc
        payload = _mapping(_load_json(path, label=label), label=label)
        if payload.get("schema_version") != EVIDENCE_SCHEMAS[label]:
            raise ValueError(f"{label} has an unsupported schema version")
        _validate_self_hash(payload, label=label)
        payloads[label] = payload
    return payloads


def validate_evidence_root(root: Path, *, framework: Mapping[str, Any]) -> dict[str, Any]:
    """Independently validate the seven file-level script 29 evidence artifacts."""

    payloads = _load_evidence_root(root)
    input_manifest = payloads["input_manifest"]
    fold_registry = payloads["fold_registry"]
    path_attestation = payloads["path_attestation"]
    benchmark_attestation = payloads["benchmark_attestation"]
    results = payloads["results"]
    decision = payloads["decision"]
    execution_validation = payloads["execution_validation"]

    for label, payload in payloads.items():
        if payload.get("model_family") != "consumer_defensive":
            raise ValueError(f"{label} has the wrong model family")
    dated_payloads = {
        label: payload for label, payload in payloads.items()
        if label != "benchmark_attestation"
    }
    _equal_binding(
        "asof_date",
        *(_iso_date(payload.get("asof_date"), label=f"{label}.asof_date") for label, payload in dated_payloads.items()),
    )
    preregistration_sha = _equal_binding(
        "preregistration_sha256",
        *(
            _digest(payload.get("preregistration_sha256"), label=f"{label}.preregistration_sha256")
            for label, payload in (
                ("input_manifest", input_manifest),
                ("fold_registry", fold_registry),
                ("path_attestation", path_attestation),
                ("results", results),
            )
        ),
    )
    candidate_sha = _equal_binding(
        "candidate_registry_sha256",
        decision.get("candidate_registry_sha256"),
        results.get("candidate_registry_sha256"),
        execution_validation.get("candidate_registry_sha256"),
    )
    _digest(candidate_sha, label="candidate_registry_sha256")
    input_sha = _equal_binding(
        "input_manifest_sha256",
        input_manifest["payload_sha256"],
        decision.get("input_panel_sha256"),
        results.get("input_manifest_sha256"),
        execution_validation.get("input_manifest_sha256"),
    )
    fold_sha = _equal_binding(
        "fold_registry_sha256",
        fold_registry["payload_sha256"],
        decision.get("fold_registry_sha256"),
        results.get("fold_registry_sha256"),
        execution_validation.get("fold_registry_sha256"),
    )
    path_sha = _equal_binding(
        "realized_path_attestation_sha256",
        path_attestation["payload_sha256"],
        fold_registry.get("realized_path_attestation_sha256"),
        results.get("realized_path_attestation_sha256"),
        execution_validation.get("realized_path_attestation_sha256"),
    )
    benchmark_sha = _equal_binding(
        "matched_benchmark_attestation_sha256",
        benchmark_attestation["payload_sha256"],
        fold_registry.get("matched_benchmark_attestation_sha256"),
        results.get("matched_benchmark_attestation_sha256"),
        execution_validation.get("matched_benchmark_attestation_sha256"),
    )
    decision_sha = _equal_binding(
        "decision_payload_sha256",
        decision["payload_sha256"],
        results.get("decision_payload_sha256"),
        execution_validation.get("decision_payload_sha256"),
    )
    expected_framework_sha = framework_sha256(framework)
    _equal_binding(
        "framework_sha256",
        expected_framework_sha,
        decision.get("framework_sha256"),
        execution_validation.get("framework_sha256"),
    )
    code_sha = _equal_binding(
        "code_sha256",
        decision.get("code_sha256"),
        execution_validation.get("code_sha256"),
    )
    for label, value in (
        ("input", input_sha),
        ("fold", fold_sha),
        ("path", path_sha),
        ("benchmark", benchmark_sha),
        ("decision", decision_sha),
        ("code", code_sha),
    ):
        _digest(value, label=f"{label}_sha256")
    if input_manifest.get("realized_path_policy") != EXPECTED_PATH_POLICY:
        raise ValueError("input manifest realized-path policy is inconsistent")
    if input_manifest["realized_path_policy"] != path_attestation.get("path_policy"):
        raise ValueError("input/path realized-policy binding failed")
    if results.get("production_promotion_enabled") is not False or results.get(
        "portfolio_write_enabled"
    ) is not False:
        raise ValueError("report-only results assert a production or Portfolio write")
    if execution_validation.get("status") != "PASS":
        raise ValueError("execution validation did not pass")
    if execution_validation.get("production_write_performed") is not False or execution_validation.get(
        "portfolio_write_performed"
    ) is not False:
        raise ValueError("execution validation reports an unauthorized write")
    if execution_validation.get("decision_sequence") != decision.get("decision_sequence"):
        raise ValueError("execution validation decision sequence mismatch")

    validate_calibration_decision(decision, framework=framework)
    result_scopes = _mapping(results.get("cohort_horizon_results"), label="results cohorts")
    fold_scopes = _mapping(fold_registry.get("cohorts"), label="fold registry cohorts")
    if set(result_scopes) != REQUIRED_COHORTS or set(fold_scopes) != REQUIRED_COHORTS:
        raise ValueError("results/folds must cover the exact cohort census")
    for cohort in sorted(REQUIRED_COHORTS):
        expected_horizons = {str(value) for value in REQUIRED_HORIZONS}
        result_horizons = _mapping(result_scopes[cohort], label=f"results.{cohort}")
        fold_horizons = _mapping(fold_scopes[cohort], label=f"folds.{cohort}")
        if set(result_horizons) != expected_horizons or set(fold_horizons) != expected_horizons:
            raise ValueError(f"{cohort}: exact result/fold horizons are required")
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            result = _mapping(result_horizons[key], label=f"results.{cohort}.{key}")
            decision_scope = decision["cohorts"][cohort]
            if result.get("performance") != decision_scope["horizon_performance"][key]:
                raise ValueError(f"{cohort}.{key}: result/decision performance mismatch")
            if result.get("evidence") != decision_scope["horizon_evidence"][key]:
                raise ValueError(f"{cohort}.{key}: result/decision evidence mismatch")
            fold_detail = _mapping(fold_horizons[key], label=f"folds.{cohort}.{key}")
            _equal_binding(
                f"{cohort}.{key}.candidate_matrix_sha256",
                fold_detail.get("candidate_matrix_sha256"),
                result["evidence"].get("candidate_matrix_sha256"),
            )
    path_count = _validate_realized_path(path_attestation, fold_registry=fold_registry)
    benchmark_cohorts = _mapping(
        benchmark_attestation.get("cohorts"), label="benchmark cohorts"
    )
    strategy_cohorts = _mapping(
        path_attestation.get("cohorts"), label="strategy cohorts"
    )
    if set(benchmark_cohorts) != REQUIRED_COHORTS:
        raise ValueError("benchmark attestation must cover the exact cohort census")
    matched: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        matched[cohort] = {}
        benchmark_horizons = _mapping(
            benchmark_cohorts[cohort], label=f"benchmark.{cohort}"
        )
        strategy_horizons = _mapping(
            strategy_cohorts[cohort], label=f"strategy.{cohort}"
        )
        for horizon in REQUIRED_HORIZONS:
            key = str(horizon)
            benchmark_rows = list(benchmark_horizons[key])
            strategy_rows = list(strategy_horizons[key])
            if len(benchmark_rows) != len(strategy_rows):
                raise ValueError(f"{cohort}/{key}: benchmark/strategy count mismatch")
            matched[cohort][key] = []
            for benchmark_row, strategy_row in zip(benchmark_rows, strategy_rows):
                if any(benchmark_row.get(field) != expected for field, expected in {
                    "signal_date": strategy_row.get("signal_date"),
                    "return_date": strategy_row.get("return_date"),
                    "strategy_observation_id": strategy_row.get("observation_id"),
                    "strategy_net_return": strategy_row.get("net_return"),
                }.items()):
                    raise ValueError(f"{cohort}/{key}: benchmark/strategy lineage mismatch")
                matched[cohort][key].append({
                    "date": benchmark_row["return_date"],
                    "strategy_net_return": benchmark_row["strategy_net_return"],
                    "primary_benchmark_return": benchmark_row["primary_benchmark_return"],
                    "xlp_return": benchmark_row["xlp_return"],
                    "spy_return": benchmark_row["spy_return"],
                })
    validate_benchmark_attestation(
        benchmark_attestation, matched_paths_by_cohort=matched
    )
    return {
        "schema_version": "consumer_defensive_calibration_file_validation_v2",
        "status": "PASS",
        "model_family": "consumer_defensive",
        "asof_date": decision["asof_date"],
        "framework_sha256": expected_framework_sha,
        "preregistration_sha256": preregistration_sha,
        "candidate_registry_sha256": candidate_sha,
        "input_manifest_sha256": input_sha,
        "fold_registry_sha256": fold_sha,
        "realized_path_attestation_sha256": path_sha,
        "matched_benchmark_attestation_sha256": benchmark_sha,
        "decision_payload_sha256": decision_sha,
        "code_sha256": code_sha,
        "realized_path_row_count": path_count,
        "production_write_performed": False,
        "portfolio_write_performed": False,
    }


def main() -> int:
    args = _parser().parse_args()
    if (args.previous_decision is not None or args.decision_history is not None) and args.decision is None:
        raise SystemExit("--previous-decision/--decision-history require --decision")
    framework = load_framework(args.framework)
    shared_contract = load_shared_service_contract(args.shared_service_contract)
    shared_hash = shared_service_contract_sha256(shared_contract)
    if shared_hash != framework["ownership"]["shared_service_contract_sha256"]:
        raise ValueError("framework is not bound to the supplied shared-service contract")
    result: dict[str, Any] = {
        "schema_version": "consumer_defensive_promotion_framework_validation_v2",
        "contract_validation_acceptance": "PASS",
        "evidence_validation_acceptance": "NOT_REQUESTED",
        "model_family": "consumer_defensive",
        "framework_sha256": framework_sha256(framework),
        "shared_service_contract_sha256": shared_hash,
        "recalibration_required": args.decision is None and args.evidence_root is None,
        "decision_valid": False,
        "evidence_valid": False,
    }
    supplied_decision: dict[str, Any] | None = None
    if args.decision is not None:
        supplied_decision = _mapping(_load_json(args.decision, label="--decision"), label="--decision")
        predecessor = (
            None
            if args.previous_decision is None
            else _mapping(_load_json(args.previous_decision, label="--previous-decision"), label="--previous-decision")
        )
        history = (
            None
            if args.decision_history is None
            else _load_json(args.decision_history, label="--decision-history")
        )
        if history is not None and not isinstance(history, list):
            raise ValueError("--decision-history must contain a JSON array")
        validate_calibration_decision(
            supplied_decision,
            framework=framework,
            previous_decision=predecessor,
            decision_history=history,
        )
        result["recalibration_required"] = False
        result["decision_valid"] = True
    if args.evidence_root is not None:
        evidence = validate_evidence_root(args.evidence_root, framework=framework)
        if supplied_decision is not None and supplied_decision["payload_sha256"] != evidence[
            "decision_payload_sha256"
        ]:
            raise ValueError("--decision and --evidence-root contain different decisions")
        result.update(
            {
                "evidence_validation_acceptance": "PASS",
                "recalibration_required": False,
                "decision_valid": True,
                "evidence_valid": True,
                "evidence": evidence,
            }
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


