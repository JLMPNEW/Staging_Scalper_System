"""Fail-closed Consumer Defensive production score publication.

This module is sector-owned.  It reads the completed Stage 6A point-in-time
feature snapshot, applies the exact preregistered calibration candidate, binds
every row to the v3 activation lock, and publishes no Portfolio Layer state.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from consumer_defensive.core.calibration_scope import (
    apply_current_production_scope,
    validate_calibration_scope_contract,
)
from consumer_defensive.core.calibration_execution_v2 import _candidate_score
from consumer_defensive.core.calibration_preregistration_v2 import (
    PORTFOLIO_POLICY,
    SCORING_POLICY,
    validate_candidate_registry,
)
from consumer_defensive.core.config import ConfigBundle, cfg_get, resolve_path
from consumer_defensive.core.promotion_engine_v3 import (
    REQUIRED_COHORTS,
    apply_activation_to_rank_rows,
    build_production_model_contract,
    canonical_sha256,
    validate_activation_registry,
)
from consumer_defensive.core.scoring_features import CORE_COMPONENT_SPECS
from consumer_defensive.core.stage7_scoring import (
    _baseline_inputs,
    _components,
    _expected_outputs,
    _verify_atomic_inputs,
    stage7_contract_sha256,
)
from consumer_defensive.core.trading_calendar_v1 import assert_one_session_lag


PUBLISHER_SCHEMA = "consumer_defensive_production_score_publisher_v3"
MANIFEST_SCHEMA = "consumer_defensive_production_score_manifest_v3"
RANK_FILENAME = "consumer_defensive_final_rank_table.csv"
MANIFEST_FILENAME = "consumer_defensive_production_score_manifest_v3.json"
MODEL_FAMILY = "consumer_defensive"
CANONICAL_SECTOR = "Consumer Staples"

# Kept locally so the Consumer producer and Portfolio consumer remain a file
# contract rather than a Python dependency.
PORTFOLIO_REQUIRED_COLUMNS = (
    "asof_date",
    "ticker",
    "company_name",
    "sector",
    "industry",
    "calibration_cohort",
    "final_score",
    "final_rank",
    "rank_ready_flag",
    "model_status",
    "score_confidence",
    "score_model_version",
    "model_version",
    "scoring_contract_version",
    "portfolio_candidate_gate",
    "portfolio_candidate_score",
    "portfolio_candidate_status",
    "portfolio_candidate_reason",
    "calibration_eligible_flag",
    "research_calibration_input_eligible_flag",
    "research_calibration_reason",
    "calibration_sample_role",
    "stage11_calibration_panel_source",
    "stage11_calibration_input_eligible_flag",
    "stage11_calibration_input_reason",
    "survivorship_corrected_panel_flag",
    "oos_score_valid_flag",
    "oos_score_asof_date",
    "oos_invalid_reason",
    "calibration_lock_date",
    "industry_aggregate",
    "promotion_state",
)
V3_LOCK_COLUMNS = (
    "consumer_defensive_production_lock_id",
    "consumer_defensive_production_lock_sha256",
    "consumer_defensive_model_contract_sha256",
    "consumer_defensive_decision_sha256",
    "consumer_defensive_selected_candidate_id",
    "consumer_defensive_deployment_state",
    "consumer_defensive_optimizer_cap",
    "consumer_defensive_confidence_multiplier",
)
PROVENANCE_COLUMNS = (
    "signal_asof_date",
    "allocation_asof_date",
    "entry_lag_trading_sessions",
    "source_input_observation_id",
    "source_stage6_contract_sha256",
    "source_component_manifest_sha256",
    "consumer_defensive_calibration_scope_sha256",
    "row_sha256",
)
RANK_COLUMNS = (*PORTFOLIO_REQUIRED_COLUMNS, *V3_LOCK_COLUMNS, *PROVENANCE_COLUMNS)

_CORE_SPEC_BY_NAME = {spec.name: spec for spec in CORE_COMPONENT_SPECS}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _digest(value: Any, *, label: str) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _strict_json_object(path: Path, *, label: str) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is missing or unsafe: {resolved}")

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON constant {value!r}")

    try:
        payload = json.loads(
            resolved.read_text(encoding="utf-8"),
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON: {resolved}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def publisher_bindings(bundle: ConfigBundle) -> dict[str, Any]:
    raw = cfg_get(bundle.payload, "production_score_publisher_v3")
    if not isinstance(raw, Mapping):
        raise ValueError("production_score_publisher_v3 config is required")
    item = dict(raw)
    if item.get("schema_version") != PUBLISHER_SCHEMA:
        raise ValueError("unsupported Consumer production-score publisher config")
    result = dict(item)
    for field in (
        "source_database_path",
        "output_root",
        "activation_registry_path",
        "candidate_registry_path",
    ):
        result[field] = resolve_path(item[field], base_dir=bundle.base_dir)
    for field in (
        "activation_registry_file_sha256",
        "activation_registry_payload_sha256",
        "candidate_registry_file_sha256",
        "candidate_registry_payload_sha256",
        "scoring_contract_version",
    ):
        result[field] = _digest(item[field], label=f"production_score_publisher_v3.{field}")
    result["entry_lag_trading_sessions"] = int(item["entry_lag_trading_sessions"])
    if result["entry_lag_trading_sessions"] != int(
        PORTFOLIO_POLICY["entry_lag_trading_sessions"]
    ) or result["entry_lag_trading_sessions"] != 1:
        raise ValueError("production publisher must preserve the preregistered one-session lag")
    for field in (
        "selected_candidate_id_by_cohort",
        "model_contract_sha256_by_cohort",
    ):
        values = item.get(field)
        if not isinstance(values, Mapping) or set(values) != REQUIRED_COHORTS:
            raise ValueError(f"production_score_publisher_v3.{field} cohort census changed")
        result[field] = dict(values)
    result["model_contract_sha256_by_cohort"] = {
        cohort: _digest(value, label=f"{cohort} model contract")
        for cohort, value in result["model_contract_sha256_by_cohort"].items()
    }
    if any(
        not isinstance(value, str) or not value.strip()
        for value in result["selected_candidate_id_by_cohort"].values()
    ):
        raise ValueError("selected candidate pins must be nonblank strings")
    return result


def load_bound_artifacts(
    *,
    activation_registry_path: Path,
    trusted_activation_registry_file_sha256: str,
    trusted_activation_registry_payload_sha256: str,
    candidate_registry_path: Path,
    trusted_candidate_registry_file_sha256: str,
    trusted_candidate_registry_payload_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    activation_path = activation_registry_path.expanduser().resolve()
    candidate_path = candidate_registry_path.expanduser().resolve()
    expected_activation_file = _digest(
        trusted_activation_registry_file_sha256,
        label="trusted activation-registry file hash",
    )
    expected_candidate_file = _digest(
        trusted_candidate_registry_file_sha256,
        label="trusted candidate-registry file hash",
    )
    observed_activation_file = file_sha256(activation_path)
    observed_candidate_file = file_sha256(candidate_path)
    if observed_activation_file != expected_activation_file:
        raise ValueError("activation-registry file SHA-256 does not match its trusted pin")
    if observed_candidate_file != expected_candidate_file:
        raise ValueError("candidate-registry file SHA-256 does not match its trusted pin")
    activation = validate_activation_registry(
        _strict_json_object(activation_path, label="activation registry")
    )
    candidates = validate_candidate_registry(
        _strict_json_object(candidate_path, label="candidate registry")
    )
    if activation["payload_sha256"] != _digest(
        trusted_activation_registry_payload_sha256,
        label="trusted activation-registry payload hash",
    ):
        raise ValueError("activation-registry payload SHA-256 does not match its trusted pin")
    if candidates["payload_sha256"] != _digest(
        trusted_candidate_registry_payload_sha256,
        label="trusted candidate-registry payload hash",
    ):
        raise ValueError("candidate-registry payload SHA-256 does not match its trusted pin")
    identities = {
        "activation_registry_path": activation_path.as_posix(),
        "activation_registry_file_sha256": observed_activation_file,
        "activation_registry_payload_sha256": activation["payload_sha256"],
        "candidate_registry_path": candidate_path.as_posix(),
        "candidate_registry_file_sha256": observed_candidate_file,
        "candidate_registry_payload_sha256": candidates["payload_sha256"],
    }
    return activation, candidates, identities


def _date(value: Any, *, label: str) -> date:
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != str(value):
        raise ValueError(f"{label} must be canonical YYYY-MM-DD")
    return parsed


def _parse_mapping(value: Any, *, label: str) -> dict[str, Any]:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON constant {constant!r}")

    try:
        parsed = json.loads(str(value), parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must decode to an object")
    return parsed


def _manifest(values: Sequence[str]) -> str:
    encoded = json.dumps(
        list(values),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def rank_row_sha256(row: Mapping[str, Any]) -> str:
    """Hash one rank row using its exact CSV-visible scalar representation."""

    expected = set(RANK_COLUMNS) - {'row_sha256'}
    observed = set(row) - {'row_sha256'}
    if observed != expected:
        raise ValueError(
            'rank row cannot be hashed with schema drift: '
            f'missing={sorted(expected - observed)} '
            f'unexpected={sorted(observed - expected)}'
        )
    body = {
        field: '' if row[field] is None else str(row[field])
        for field in RANK_COLUMNS
        if field != 'row_sha256'
    }
    return canonical_sha256(body)


def _cohort_contracts(
    *,
    activation_registry: Mapping[str, Any],
    candidate_registry: Mapping[str, Any],
    bindings: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    registry = validate_activation_registry(activation_registry)
    candidates = validate_candidate_registry(candidate_registry)
    by_id = {str(row["candidate_id"]): dict(row) for row in candidates["candidates"]}
    selected: dict[str, dict[str, Any]] = {}
    contracts: dict[str, dict[str, Any]] = {}
    for cohort in sorted(REQUIRED_COHORTS):
        lock = registry["cohorts"][cohort]
        candidate_id = str(lock["selected_candidate_id"])
        if candidate_id != bindings["selected_candidate_id_by_cohort"][cohort]:
            raise ValueError(f"{cohort}: activation candidate differs from the Consumer config pin")
        candidate = by_id.get(candidate_id)
        if candidate is None:
            raise ValueError(f"{cohort}: selected candidate is absent from the pinned registry")
        if candidate["cohort"] != cohort or int(candidate["horizon_sessions"]) != 63:
            raise ValueError(f"{cohort}: selected candidate scope/horizon is inconsistent")
        if candidate["specialized_weights"]:
            raise ValueError(
                f"{cohort}: selected contract requires specialized production scores; "
                "Stage 6A provides measurement-only specialized values"
            )
        contract = build_production_model_contract(
            cohort=cohort,
            selected_candidate_id=candidate_id,
            candidate_definition=candidate,
            candidate_registry_sha256=candidates["payload_sha256"],
            score_model_version=str(lock["score_model_version"]),
            scoring_contract_version=str(lock["scoring_contract_version"]),
        )
        expected_contract = bindings["model_contract_sha256_by_cohort"][cohort]
        if contract["payload_sha256"] != lock["model_contract_sha256"]:
            raise ValueError(f"{cohort}: reconstructed model contract does not match activation lock")
        if contract["payload_sha256"] != expected_contract:
            raise ValueError(f"{cohort}: reconstructed model contract does not match config pin")
        if lock["scoring_contract_version"] != bindings["scoring_contract_version"]:
            raise ValueError(f"{cohort}: scoring contract differs from the Consumer config pin")
        selected[cohort] = candidate
        contracts[cohort] = contract
    return selected, contracts


def _candidate_confidence(row: Mapping[str, Any], candidate: Mapping[str, Any], *, short_birth: str) -> float:
    available = 0.0
    applicable = 0.0
    for name, raw_weight in candidate["core_weights"].items():
        weight = float(raw_weight)
        spec = _CORE_SPEC_BY_NAME[name]
        structurally_unavailable = (
            spec.rank_requirement == "any_short" and str(row["asof_date"]) < short_birth
        )
        if spec.rank_requirement == "optional" or structurally_unavailable:
            continue
        applicable += weight
        value = row["_component_scores"].get(name)
        usable = (
            float(row["_component_quality"].get(name, 0.0)) > 0.0
            and value is not None
            and math.isfinite(float(value))
        )
        if usable:
            available += weight
    for name, raw_weight in candidate["specialized_weights"].items():
        weight = float(raw_weight)
        applicable += weight
        value = row["_specialized_scores"].get(name)
        if value is not None and math.isfinite(float(value)):
            available += weight
    return 0.0 if applicable <= 0.0 else available / applicable


def _taxonomy_rows(
    conn: sqlite3.Connection, *, signal_asof_date: str
) -> dict[str, dict[str, Any]]:
    rows = [
        dict(row)
        for row in conn.execute(
            """SELECT t.ticker,t.calibration_cohort_id,t.calibration_cohort,
                      t.applicability_subtype,c.company_name
               FROM dim_consumer_defensive_taxonomy t
               JOIN dim_company c ON c.company_id=t.company_id
               JOIN dim_universe_membership m
                 ON m.ticker=t.ticker AND m.model_family=t.model_family
               WHERE t.model_family='consumer_defensive'
                 AND m.live_investable_flag=1
                 AND m.start_date<=?
                 AND COALESCE(m.end_date,'9999-12-31')>=?
               ORDER BY t.ticker""",
            (signal_asof_date, signal_asof_date),
        )
    ]
    result = {str(row["ticker"]): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("live Consumer taxonomy contains duplicate tickers")
    return result


def _rank(rows: list[dict[str, Any]]) -> None:
    rankable = sorted(
        (row for row in rows if int(row["rank_ready_flag"]) == 1),
        key=lambda row: (-float(row["final_score"]), str(row["ticker"])),
    )
    for position, row in enumerate(rankable, start=1):
        row["final_rank"] = position
    for row in rows:
        row.setdefault("final_rank", "")


def build_production_rank_rows(
    conn: sqlite3.Connection,
    bundle: ConfigBundle,
    *,
    signal_asof_date: str,
    allocation_asof_date: str,
    activation_registry: Mapping[str, Any],
    candidate_registry: Mapping[str, Any],
    bindings: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build lock-bound rows without reading outcomes or mutating SQLite."""

    signal = _date(signal_asof_date, label="signal_asof_date")
    allocation = _date(allocation_asof_date, label="allocation_asof_date")
    assert_one_session_lag(
        signal_asof_date=signal.isoformat(),
        allocation_asof_date=allocation.isoformat(),
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
        raise RuntimeError("production score publisher requires SQLite query_only mode")
    if not conn.in_transaction:
        raise RuntimeError(
            "production score publisher requires one caller-owned SQLite read transaction"
        )
    resolved_bindings = dict(bindings or publisher_bindings(bundle))
    activation = validate_activation_registry(activation_registry)
    candidates = validate_candidate_registry(candidate_registry)
    if not (
        _date(activation["effective_from"], label="activation effective_from")
        <= allocation
        <= _date(activation["valid_until"], label="activation valid_until")
    ):
        raise ValueError("allocation_asof_date is outside activation-registry authority")
    if _date(activation["asof_date"], label="activation asof_date") > signal:
        raise ValueError("signal features predate the activation decision evidence")
    if _date(candidates["asof_date"], label="candidate registry asof_date") > signal:
        raise ValueError("signal features predate the selected candidate registry")
    if int(resolved_bindings["entry_lag_trading_sessions"]) != int(
        PORTFOLIO_POLICY["entry_lag_trading_sessions"]
    ):
        raise ValueError("entry-lag contract differs from calibration preregistration")

    latest = conn.execute(
        """SELECT MAX(asof_date) FROM feature_scoring_input
           WHERE model_family='consumer_defensive' AND asof_date<?""",
        (allocation_asof_date,),
    ).fetchone()[0]
    if str(latest or "") != signal_asof_date:
        raise ValueError(
            "signal_asof_date must equal the latest completed Stage 6A PIT feature snapshot"
        )
    baseline_source = str(cfg_get(bundle.payload, "stage7_scoring.baseline_source_id"))
    inputs = _baseline_inputs(
        conn, as_of=signal_asof_date, baseline_source_id=baseline_source
    )
    components = _components(conn, as_of=signal_asof_date)
    selected, contracts = _cohort_contracts(
        activation_registry=activation,
        candidate_registry=candidates,
        bindings=resolved_bindings,
    )
    specialized_weighted_cohorts = sorted(
        cohort
        for cohort, candidate in selected.items()
        if candidate["specialized_weights"]
    )
    _verify_atomic_inputs(
        conn,
        bundle,
        as_of=signal_asof_date,
        inputs=inputs,
        components=components,
        # Production contracts are reconstructed and checked above. The current
        # v3 contracts are core-only and explicitly reject specialized weights,
        # so a measurement-only Stage 6B overlay cannot affect their score,
        # eligibility, or confidence. Stage 7 retains the strict default.
        require_stage6b_overlay=bool(specialized_weighted_cohorts),
    )
    baseline = _expected_outputs(
        bundle,
        as_of=signal_asof_date,
        contract_sha=stage7_contract_sha256(bundle),
        inputs=inputs,
        components=components,
    )
    input_by_ticker = {str(row["ticker"]): row for row in inputs}
    baseline_by_ticker = {str(row["ticker"]): row for row in baseline}
    component_by_ticker: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in components:
        component_by_ticker[str(row["ticker"])][str(row["component_name"])] = row
    source_taxonomy = _taxonomy_rows(conn, signal_asof_date=signal_asof_date)
    scoped_taxonomy_rows, scope_summary = apply_current_production_scope(
        list(source_taxonomy.values()),
        bundle,
    )
    taxonomy = {
        str(row["ticker"]): row for row in scoped_taxonomy_rows
    }
    if set(input_by_ticker) != set(taxonomy) or set(input_by_ticker) != set(baseline_by_ticker):
        raise ValueError(
            "current feature, reviewed taxonomy scope, and score-input ticker "
            "censuses differ"
        )

    short_birth = str(cfg_get(bundle.payload, "positioning.source_birthdates.short_interest"))
    minimum_quality = float(SCORING_POLICY["minimum_data_quality_confidence"])
    maximum_missing = float(SCORING_POLICY["maximum_missing_component_weight"])
    if not math.isclose(
        float(cfg_get(bundle.payload, "stage7_scoring.minimum_data_quality_confidence")),
        minimum_quality,
        rel_tol=0.0,
        abs_tol=1e-12,
    ) or not math.isclose(
        float(cfg_get(bundle.payload, "stage7_scoring.maximum_missing_component_weight")),
        maximum_missing,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("live scoring quality guards differ from calibration preregistration")

    rows: list[dict[str, Any]] = []
    for ticker in sorted(input_by_ticker):
        input_row = input_by_ticker[ticker]
        source = baseline_by_ticker[ticker]
        taxonomy_row = taxonomy[ticker]
        cohort = str(taxonomy_row["calibration_cohort_id"])
        if cohort not in REQUIRED_COHORTS:
            raise ValueError(f"{ticker}: unsupported calibration cohort {cohort!r}")
        candidate = selected[cohort]
        core_components = component_by_ticker[ticker]
        component_ids = sorted(
            str(core_components[name]["component_observation_id"])
            for name in candidate["core_weights"]
        )
        prepared = {
            "asof_date": signal_asof_date,
            "ticker": ticker,
            "membership_eligible_flag": int(input_row["calibration_eligible_flag"]),
            "investable_flag": int(input_row["rank_ready_flag"]),
            "_component_scores": _parse_mapping(
                source["component_scores_json"], label=f"{ticker} component scores"
            ),
            "_component_quality": _parse_mapping(
                source["component_quality_json"], label=f"{ticker} component quality"
            ),
            "_component_raw_values": {
                name: core_components[name]["raw_value"] for name in candidate["core_weights"]
            },
            "_specialized_scores": {},
        }
        score, eligible = _candidate_score(
            prepared,
            candidate,
            short_interest_birthdate=short_birth,
            minimum_quality=minimum_quality,
            maximum_missing=maximum_missing,
        )
        confidence = _candidate_confidence(prepared, candidate, short_birth=short_birth)
        reason = "ok" if eligible else str(
            input_row["review_reason"] or "candidate_quality_or_component_gate"
        )
        contract = contracts[cohort]
        row = {
            "asof_date": allocation_asof_date,
            "ticker": ticker,
            "company_name": str(taxonomy_row["company_name"]),
            "sector": CANONICAL_SECTOR,
            "industry": str(taxonomy_row["calibration_cohort"]),
            "calibration_cohort": cohort,
            "final_score": score,
            "final_rank": "",
            "rank_ready_flag": int(eligible),
            "model_status": "complete" if eligible else "review_required",
            "score_confidence": confidence,
            "score_model_version": contract["score_model_version"],
            "model_version": contract["score_model_version"],
            "scoring_contract_version": contract["scoring_contract_version"],
            "portfolio_candidate_gate": 0,
            "portfolio_candidate_score": score,
            "portfolio_candidate_status": "pending_activation",
            "portfolio_candidate_reason": reason,
            "calibration_eligible_flag": int(eligible),
            "research_calibration_input_eligible_flag": int(eligible),
            "research_calibration_reason": reason,
            "calibration_sample_role": "strict_oos" if eligible else "excluded",
            "stage11_calibration_panel_source": "consumer_defensive_live_production_score_v3",
            "stage11_calibration_input_eligible_flag": int(eligible),
            "stage11_calibration_input_reason": reason,
            "survivorship_corrected_panel_flag": 0,
            "oos_score_valid_flag": int(eligible),
            "oos_score_asof_date": signal_asof_date if eligible else "",
            "oos_invalid_reason": "" if eligible else reason,
            "calibration_lock_date": str(activation["asof_date"]),
            "industry_aggregate": CANONICAL_SECTOR,
            "promotion_state": "shadow_monitor",
            "consumer_defensive_production_lock_id": "",
            "consumer_defensive_production_lock_sha256": "",
            "consumer_defensive_model_contract_sha256": contract["payload_sha256"],
            "consumer_defensive_decision_sha256": "",
            "consumer_defensive_selected_candidate_id": candidate["candidate_id"],
            "consumer_defensive_deployment_state": "",
            "consumer_defensive_optimizer_cap": 0.0,
            "consumer_defensive_confidence_multiplier": 0.0,
            "signal_asof_date": signal_asof_date,
            "allocation_asof_date": allocation_asof_date,
            "entry_lag_trading_sessions": int(
                resolved_bindings["entry_lag_trading_sessions"]
            ),
            "source_input_observation_id": str(input_row["input_observation_id"]),
            "source_stage6_contract_sha256": str(input_row["contract_sha256"]),
            "source_component_manifest_sha256": _manifest(component_ids),
            "consumer_defensive_calibration_scope_sha256": scope_summary[
                "contract"
            ]["payload_sha256"],
        }
        rows.append(row)
    _rank(rows)
    activated = apply_activation_to_rank_rows(rows, activation_registry=activation)
    if not activated or not any(int(row["oos_score_valid_flag"]) == 1 for row in activated):
        raise ValueError("production publisher produced no OOS-valid rows")
    ready_by_cohort = {cohort: 0 for cohort in REQUIRED_COHORTS}
    for row in activated:
        if int(row["rank_ready_flag"]) == 1:
            ready_by_cohort[str(row["calibration_cohort"])] += 1
        row["row_sha256"] = rank_row_sha256(row)
        if set(row) != set(RANK_COLUMNS):
            raise RuntimeError("production rank-row schema drifted")
    if any(count == 0 for count in ready_by_cohort.values()):
        raise ValueError(f"at least one Consumer cohort has no rank-ready rows: {ready_by_cohort}")
    activated.sort(
        key=lambda row: (
            int(row["rank_ready_flag"]) != 1,
            int(row["final_rank"]) if row["final_rank"] != "" else 10**9,
            str(row["ticker"]),
        )
    )
    source = {
        "signal_asof_date": signal_asof_date,
        "allocation_asof_date": allocation_asof_date,
        "entry_lag_trading_sessions": int(resolved_bindings["entry_lag_trading_sessions"]),
        "source_input_count": len(inputs),
        "source_component_count": len(components),
        "source_live_ticker_count": scope_summary["source_ticker_count"],
        "source_live_tickers_sha256": scope_summary["source_tickers_sha256"],
        "calibration_scope_contract": scope_summary["contract"],
        "calibration_scope_sha256": scope_summary["contract"]["payload_sha256"],
        "observed_excluded_tickers": scope_summary["observed_excluded_tickers"],
        "observed_excluded_ticker_count": scope_summary[
            "observed_excluded_ticker_count"
        ],
        "published_ticker_count": scope_summary["remaining_ticker_count"],
        "published_tickers_sha256": scope_summary["remaining_tickers_sha256"],
        "published_tickers_by_cohort": scope_summary[
            "remaining_tickers_by_cohort"
        ],
        "source_input_manifest_sha256": _manifest(
            sorted(str(row["input_observation_id"]) for row in inputs)
        ),
        "source_component_manifest_sha256": _manifest(
            sorted(str(row["component_observation_id"]) for row in components)
        ),
        "stage6b_overlay_required": bool(specialized_weighted_cohorts),
        "specialized_weighted_cohorts": specialized_weighted_cohorts,
        "rank_ready_by_cohort": dict(sorted(ready_by_cohort.items())),
        "model_contract_sha256_by_cohort": {
            cohort: contracts[cohort]["payload_sha256"]
            for cohort in sorted(contracts)
        },
    }
    return activated, source


def _csv_text(rows: Sequence[Mapping[str, Any]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=RANK_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        if set(row) != set(RANK_COLUMNS):
            raise ValueError("rank row does not match the exact output schema")
        writer.writerow({field: row[field] for field in RANK_COLUMNS})
    return buffer.getvalue()


def _json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"


def _assert_output_path_safe(path: Path) -> None:
    current = path.expanduser().resolve().parent
    while current != current.parent:
        if current.exists() and current.is_symlink():
            raise RuntimeError(f"refusing symlinked production output path: {current}")
        current = current.parent


def publish_immutable_text(path: Path, text: str) -> None:
    resolved = path.expanduser().resolve()
    _assert_output_path_safe(resolved)
    encoded = text.encode("utf-8")
    if resolved.exists():
        if not resolved.is_file() or resolved.is_symlink():
            raise RuntimeError(f"immutable output target is unsafe: {resolved}")
        if resolved.read_bytes() != encoded:
            raise FileExistsError(f"immutable output already exists with different bytes: {resolved}")
        return
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary_key = hashlib.sha256(
        resolved.name.encode("utf-8") + b"\0" + encoded
    ).hexdigest()[:16]
    temporary = resolved.with_name(f".publish-{temporary_key}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, resolved)
        except FileExistsError:
            if resolved.read_bytes() != encoded:
                raise FileExistsError(
                    f"immutable output was concurrently published with different bytes: {resolved}"
                )
    finally:
        if temporary.exists():
            temporary.unlink()


def publish_production_scores(
    *,
    output_root: Path,
    allocation_asof_date: str,
    rows: Sequence[Mapping[str, Any]],
    source: Mapping[str, Any],
    artifact_identities: Mapping[str, str],
    source_database_path: Path,
) -> dict[str, Any]:
    allocation = _date(allocation_asof_date, label="allocation_asof_date").isoformat()
    scope_contract = validate_calibration_scope_contract(
        dict(source.get("calibration_scope_contract") or {})
    )
    scope_sha = _digest(
        source.get("calibration_scope_sha256"),
        label="calibration scope hash",
    )
    if canonical_sha256(scope_contract) != scope_sha or (
        str(scope_contract.get("payload_sha256") or "") != scope_sha
    ):
        raise ValueError("calibration scope contract failed its self-hash tie-out")
    raw_tickers = [str(row.get("ticker") or "").strip() for row in rows]
    row_tickers = [ticker.upper() for ticker in raw_tickers]
    if (
        any(not ticker for ticker in row_tickers)
        or raw_tickers != row_tickers
        or len(row_tickers) != len(set(row_tickers))
        or len(row_tickers) != int(source.get("published_ticker_count", -1))
    ):
        raise ValueError("published ticker census does not match the reviewed scope")
    excluded = set(str(value) for value in scope_contract.get("excluded_tickers", []))
    leaked = sorted(excluded.intersection(row_tickers))
    if leaked:
        raise ValueError(
            "production rank rows contain reviewed exclusions: "
            + ", ".join(leaked)
        )
    observed_ticker_sha = _manifest(sorted(row_tickers))
    if (
        observed_ticker_sha
        != str(source.get("published_tickers_sha256") or "")
        or observed_ticker_sha
        != str(scope_contract["expected_remaining_current_tickers_sha256"])
    ):
        raise ValueError("published ticker census does not match the reviewed scope")
    if any(
        str(row.get("consumer_defensive_calibration_scope_sha256") or "")
        != scope_sha
        for row in rows
    ):
        raise ValueError("production rank rows are not bound to the reviewed scope")
    expected_by_cohort = {
        str(cohort): int(count)
        for cohort, count in scope_contract[
            "expected_remaining_current_by_cohort"
        ].items()
    }
    observed_by_cohort = {
        cohort: sum(
            str(row.get("calibration_cohort") or "").strip() == cohort
            for row in rows
        )
        for cohort in sorted(expected_by_cohort)
    }
    if (
        len(rows)
        != int(scope_contract["expected_remaining_current_ticker_count"])
        or observed_by_cohort != expected_by_cohort
        or dict(source.get("published_tickers_by_cohort") or {})
        != observed_by_cohort
    ):
        raise ValueError("published cohort census differs from the reviewed scope")
    expected_source_count = len(rows) + int(scope_contract["excluded_ticker_count"])
    if (
        int(source.get("source_live_ticker_count", -1))
        != expected_source_count
        or list(source.get("observed_excluded_tickers") or [])
        != list(scope_contract["excluded_tickers"])
        or int(source.get("observed_excluded_ticker_count", -1))
        != int(scope_contract["excluded_ticker_count"])
    ):
        raise ValueError("source-universe exclusion census does not tie")
    stale_row_hashes = [
        ticker
        for ticker, row in zip(row_tickers, rows, strict=True)
        if str(row.get("row_sha256") or "") != rank_row_sha256(row)
    ]
    if stale_row_hashes:
        raise ValueError(
            "production rank rows failed their self-hash: "
            + ", ".join(stale_row_hashes[:10])
        )
    database_file_sha256 = _digest(
        source.get("source_database_file_sha256"),
        label="source database file hash",
    )
    raw_wal_sha256 = str(source.get("source_database_wal_file_sha256") or "")
    database_wal_file_sha256 = (
        _digest(raw_wal_sha256, label="source database WAL file hash")
        if raw_wal_sha256
        else ""
    )
    database_data_version = int(source.get("source_database_data_version", -1))
    if database_data_version < 0:
        raise ValueError("source database data_version must be nonnegative")
    dated = output_root.expanduser().resolve() / "consumer_defensive" / "dashboard" / allocation
    csv_path = dated / RANK_FILENAME
    manifest_path = dated / MANIFEST_FILENAME
    csv_text = _csv_text(rows)
    csv_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    publish_immutable_text(csv_path, csv_text)
    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "status": "PASS",
        "model_family": MODEL_FAMILY,
        "allocation_asof_date": allocation,
        "signal_asof_date": str(source["signal_asof_date"]),
        "entry_lag_trading_sessions": int(source["entry_lag_trading_sessions"]),
        "rank_csv_path": csv_path.as_posix(),
        "rank_csv_file_sha256": csv_sha,
        "rank_row_count": len(rows),
        "rank_ready_count": sum(int(row["rank_ready_flag"]) for row in rows),
        "oos_valid_count": sum(int(row["oos_score_valid_flag"]) for row in rows),
        "source_database_path": source_database_path.expanduser().resolve().as_posix(),
        "source_database_file_sha256": database_file_sha256,
        "source_database_wal_file_sha256": database_wal_file_sha256,
        "source_database_data_version": database_data_version,
        "source_input_count": int(source["source_input_count"]),
        "source_component_count": int(source["source_component_count"]),
        "source_input_manifest_sha256": str(source["source_input_manifest_sha256"]),
        "source_component_manifest_sha256": str(source["source_component_manifest_sha256"]),
        "source_live_ticker_count": int(source["source_live_ticker_count"]),
        "source_live_tickers_sha256": str(source["source_live_tickers_sha256"]),
        "calibration_scope_contract": dict(source["calibration_scope_contract"]),
        "calibration_scope_sha256": str(source["calibration_scope_sha256"]),
        "observed_excluded_tickers": list(source["observed_excluded_tickers"]),
        "observed_excluded_ticker_count": int(
            source["observed_excluded_ticker_count"]
        ),
        "published_ticker_count": int(source["published_ticker_count"]),
        "published_tickers_sha256": str(source["published_tickers_sha256"]),
        "published_tickers_by_cohort": dict(source["published_tickers_by_cohort"]),
        "stage6b_overlay_required": bool(source["stage6b_overlay_required"]),
        "specialized_weighted_cohorts": list(source["specialized_weighted_cohorts"]),
        "rank_ready_by_cohort": dict(source["rank_ready_by_cohort"]),
        "model_contract_sha256_by_cohort": dict(
            source["model_contract_sha256_by_cohort"]
        ),
        **dict(artifact_identities),
        "database_access_mode": "read_only",
        "database_write_count": 0,
        "portfolio_write_performed": False,
    }
    manifest["payload_sha256"] = canonical_sha256(manifest)
    publish_immutable_text(manifest_path, _json_text(manifest))
    if file_sha256(csv_path) != csv_sha:
        raise RuntimeError("published production rank CSV failed its byte hash tie-out")
    if _strict_json_object(manifest_path, label="published score manifest") != manifest:
        raise RuntimeError("published production score manifest failed exact recomputation")
    return manifest


__all__ = [
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA",
    "PUBLISHER_SCHEMA",
    "RANK_COLUMNS",
    "RANK_FILENAME",
    "build_production_rank_rows",
    "file_sha256",
    "load_bound_artifacts",
    "publish_immutable_text",
    "publish_production_scores",
    "publisher_bindings",
    "rank_row_sha256",
]
