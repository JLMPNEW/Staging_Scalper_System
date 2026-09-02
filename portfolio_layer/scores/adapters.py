"""Per-sector adapters: read a sector's published score CSV and map it onto CanonicalScore rows.

Four shapes cover all sub-sectors:
  - tech_family : semiconductors, software_infrastructure, technology_hardware (shared `final_score`)
  - industrial_family : industrials final-rank tables using the shared `final_score` contract
  - transportation_subgroup : Transportation v8 rows with sealed group recipes
  - consumer_defensive : Consumer Staples final-rank tables with explicit promotion governance
  - biotech     : biotech_index daily scores using `portfolio_candidate_*` when present
  - med_devices : med_device daily composite scores gated by `portfolio_candidate_gate`

Adapters read files only. They never import a sector package or open a sector DB.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from orchestration_contracts.financial_lineage import (
    financial_lineage_sidecar_alignment_errors,
    lineage_row_is_safe,
    POLICY_CANDIDATE_ONLY,
    POLICY_DISABLED,
    POLICY_STRICT_UNIVERSE,
    policy_for_model_family,
)
from portfolio_layer.core.contracts import AdapterResult, CanonicalScore
from portfolio_layer.core.contracts import FINANCIAL_LINEAGE_FIELDS
from portfolio_layer.core.contracts import read_csv

LOGGER = logging.getLogger(__name__)

INDUSTRIAL_FAMILY_REQUIRED_COLUMNS = (
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
)

INDUSTRIAL_FAMILY_ONE_OF_COLUMNS = (
    ("industry_aggregate", "subsector"),
    ("score_confidence", "data_quality_confidence"),
)


# Keep this schema local to Portfolio Layer. The adapter is deliberately a
# file-contract boundary: importing the sector package (or opening its DB)
# would couple portfolio construction to a mutable upstream implementation.
CONSUMER_DEFENSIVE_REQUIRED_COLUMNS = (
    *INDUSTRIAL_FAMILY_REQUIRED_COLUMNS,
    'industry_aggregate',
    'promotion_state',
)

CONSUMER_DEFENSIVE_PROMOTION_STATES = frozenset(
    {'promoted', 'shadow_monitor', 'deferred'}
)
CONSUMER_DEFENSIVE_COHORTS = frozenset(
    {
        "beverages",
        "consumer_staples_distribution_retail",
        "household_personal_tobacco",
        "packaged_foods_agricultural_products",
    }
)
# Portfolio Layer accepts only the standard-allocation state from the current
# Consumer Defensive production contract. The retired canary/pilot/scaled
# ladder must not be reintroduced by pinning an old, otherwise validly hashed
# registry. Non-promoted cohorts keep their equal-budget slot in cash.
CONSUMER_DEFENSIVE_V3_STANDARD_STATE = "active_full"
CONSUMER_DEFENSIVE_V3_NON_DEPLOYABLE_STATES = frozenset(
    {"rollback", "benchmark_production"}
)
CONSUMER_DEFENSIVE_V3_ALLOWED_STATES = frozenset(
    {
        CONSUMER_DEFENSIVE_V3_STANDARD_STATE,
        *CONSUMER_DEFENSIVE_V3_NON_DEPLOYABLE_STATES,
    }
)
CONSUMER_DEFENSIVE_V3_REGISTRY_KEYS = frozenset(
    {
        "schema_version", "model_family", "asof_date", "effective_from",
        "valid_until", "maximum_activation_age_days", "decision_sha256",
        "framework_sha256", "source_input_sha256", "calibration_write_performed",
        "portfolio_write_performed", "cohorts", "payload_sha256",
    }
)
CONSUMER_DEFENSIVE_V3_LOCK_KEYS = frozenset(
    {
        "schema_version", "model_family", "cohort", "lock_id", "effective_from",
        "valid_until", "deployment_state", "promotion_state", "investable",
        "tier_deployment_fraction", "effective_deployment_fraction",
        "approved_full_portfolio_cap", "optimizer_cap", "expected_alpha_at_full",
        "confidence_multiplier", "decision_sha256", "framework_sha256",
        "source_input_sha256", "model_contract_sha256", "selected_candidate_id",
        "score_model_version", "scoring_contract_version", "payload_sha256",
    }
)
CONSUMER_DEFENSIVE_V3_ROW_LOCK_FIELDS = {
    "consumer_defensive_production_lock_id": "lock_id",
    "consumer_defensive_production_lock_sha256": "payload_sha256",
    "consumer_defensive_model_contract_sha256": "model_contract_sha256",
    "consumer_defensive_decision_sha256": "decision_sha256",
    "consumer_defensive_selected_candidate_id": "selected_candidate_id",
    "consumer_defensive_deployment_state": "deployment_state",
}
CONSUMER_DEFENSIVE_CALIBRATION_SCOPE_KEYS = frozenset(
    {
        "mode",
        "enforcement_stage",
        "selection_basis",
        "evidence_classification",
        "strict_oos_eligible",
        "preserve_source_history",
        "production_promotion_requires_fresh_post_scope_evidence",
        "reviewed_as_of",
        "excluded_tickers_by_cohort",
        "excluded_tickers",
        "excluded_ticker_count",
        "expected_remaining_current_ticker_count",
        "expected_remaining_current_tickers_sha256",
        "expected_remaining_current_by_cohort",
        "payload_sha256",
    }
)
CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS = (
    *CONSUMER_DEFENSIVE_REQUIRED_COLUMNS,
    *CONSUMER_DEFENSIVE_V3_ROW_LOCK_FIELDS,
    "consumer_defensive_optimizer_cap",
    "consumer_defensive_confidence_multiplier",
    "signal_asof_date",
    "allocation_asof_date",
    "entry_lag_trading_sessions",
    "source_input_observation_id",
    "source_stage6_contract_sha256",
    "source_component_manifest_sha256",
    "consumer_defensive_calibration_scope_sha256",
    "row_sha256",
)

TRANSPORTATION_SUBGROUP_REQUIRED_COLUMNS = (
    *INDUSTRIAL_FAMILY_REQUIRED_COLUMNS,
    "industry_aggregate",
    "transportation_scoring_mode",
    "transportation_production_state",
    "transportation_group_recipe_version",
    "transportation_subgroup_policy_sha256",
    "transportation_cohort_id",
    "transportation_group_id",
    "transportation_group_recipe_key",
    "transportation_group_recipe_sha256",
    "transportation_group_ranking_mode",
    "transportation_group_aggregate_weight",
    "transportation_group_rank",
    "transportation_membership_scope",
    "transportation_membership_effective_from",
    "transportation_membership_effective_to",
    "transportation_component_recipe_state",
    "transportation_applied_component_weights_sha256",
    "transportation_subgroup_score_sha256",
    "transportation_expected_group_count",
    "transportation_expected_ticker_count",
    "transportation_group_expected_ticker_count",
    "transportation_production_lock_id",
    "transportation_decision_manifest_sha256",
)
TRANSPORTATION_SUBGROUP_SHA_FIELDS = (
    "transportation_subgroup_policy_sha256",
    "transportation_group_recipe_sha256",
    "transportation_applied_component_weights_sha256",
    "transportation_subgroup_score_sha256",
)
TRANSPORTATION_SUBGROUP_STATES = frozenset({"shadow", "promoted"})


def _f(value: Any) -> float | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "1.0", "true", "yes", "y", "t"):
        return True
    if text in ("", "0", "0.0", "false", "no", "n", "f", "none", "null", "nan"):
        return False
    numeric = _f(text)
    if numeric is not None:
        return numeric != 0.0
    normalized = re.sub(r"[^a-z]", "", text)
    if normalized in ("true", "yes", "y", "t"):
        return True
    if normalized in ("false", "no", "n", "f"):
        return False
    return False


def _dominant(rows: list[dict[str, str]], column: str, default: str = "") -> str:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(column, "")).strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return default
    return max(counts, key=lambda value: (counts[value], value))


def _first_text(row: dict[str, str], columns: tuple[str, ...]) -> str:
    for column in columns:
        value = str(row.get(column, "")).strip()
        if value:
            return value
    return ""


def _source_asof(row: dict[str, str]) -> str:
    return _first_text(row, ("asof_date", "as_of_date", "snapshot_date", "score_asof_date"))


def _confidence(value: Any, *, default: float = 0.5, denominator: float | None = None) -> float:
    raw = _f(value)
    if raw is None:
        return default
    if denominator is not None:
        raw = raw / denominator
    return max(0.0, min(1.0, raw))


def _research_reason(*, eligible: bool, reason: str) -> str:
    return "ok" if eligible else (reason or "not_calibration_research_eligible")


def _oos_score_valid(row: dict[str, str], cfg: dict[str, Any], *, default_requires_oos: bool = True) -> bool:
    require_oos = bool(cfg.get("require_oos_score_valid", default_requires_oos))
    if "oos_score_valid_flag" in row:
        # the column exists: a blank cell is a missing assertion and fails CLOSED (not oos-valid),
        # rather than falling through to the column-absent policy default
        return _truthy(row.get("oos_score_valid_flag"))
    return not require_oos


def _sample_role(row: dict[str, str], *, investable: bool, research_eligible: bool) -> str:
    role = str(row.get("calibration_sample_role") or row.get("calibration_status") or "").strip().lower()
    if role in {"strict_oos", "pre_lock_research", "excluded"}:
        return role
    normalized = re.sub(r"[^a-z0-9]", "", role)
    if normalized in {"strictoos", "oos", "oosvalid", "production", "productionvalid", "livevalid"}:
        return "strict_oos"
    if normalized in {
        "prelockresearch",
        "researchcalibrationinput",
        "validresearchcalibrationinput",
        "researchinput",
        "researcheligible",
    }:
        return "strict_oos" if _truthy(row.get("oos_score_valid_flag")) else "pre_lock_research"
    if normalized in {
        "excluded",
        "excludedfromresearchcalibration",
        "notcalibrationeligible",
        "missingpricedata",
        "missingscore",
    }:
        return "excluded"
    if _truthy(row.get("oos_score_valid_flag")):
        return "strict_oos"
    if _truthy(row.get("stage11_calibration_input_eligible_flag")) or _truthy(
        row.get("research_calibration_input_eligible_flag")
    ):
        return "pre_lock_research"
    if not research_eligible:
        return "excluded"
    return "strict_oos" if investable else "pre_lock_research"


def _survivorship_corrected(row: dict[str, str]) -> bool:
    return _truthy(row.get("survivorship_corrected_panel_flag"))


def _stage11_research_allowed(row: dict[str, str], *, source_role: str, oos_score_valid: bool) -> bool:
    if source_role == "excluded":
        # an explicit source-level exclusion wins over contradictory eligibility flags (fail closed)
        return False
    return bool((source_role == "strict_oos" and oos_score_valid) or _survivorship_corrected(row))


def _research_guard_reason(row: dict[str, str], *, source_role: str, oos_score_valid: bool) -> str:
    if source_role == "excluded":
        return "source_role_excluded"
    if source_role != "strict_oos" and not _survivorship_corrected(row):
        return "dashboard_snapshot_not_survivorship_corrected_use_sector_diagnostics_panel"
    if source_role == "strict_oos" and not oos_score_valid:
        return f"not_oos_score_valid:{str(row.get('oos_invalid_reason') or '').strip()[:180]}"
    return "not_calibration_research_eligible"


def _contract_sample_role(
    source_role: str,
    *,
    investable: bool,
    research_eligible: bool,
    oos_score_valid: bool,
) -> str:
    if investable:
        return "strict_oos" if oos_score_valid else "excluded"
    if not research_eligible:
        return "excluded"
    if source_role == "strict_oos" and oos_score_valid:
        return "strict_oos"
    return "pre_lock_research"


def dated_candidates(cfg: dict[str, Any], root: Path) -> list[tuple[str, Path]]:
    """All existing dated score files for a dated-mode sector config, as (yyyymmdd, path) ascending.

    The date token occupies one whole path segment (the dated folder name). Existing biotech outputs
    use YYYYMMDD; newer technology snapshots use YYYY-MM-DD. Shared by Stage 1 resolution and the
    Stage 11 snapshot-store replay driver so both see the identical universe of dated files.
    """
    rel = str(cfg["file_path"])
    parts = rel.split("/")
    token = ""
    idx = -1
    for i, p in enumerate(parts):
        if "{yyyymmdd}" in p:
            token = "{yyyymmdd}"
            idx = i
            break
        if "{yyyy-mm-dd}" in p:
            token = "{yyyy-mm-dd}"
            idx = i
            break
    if idx < 0:
        raise ValueError(f"dated file_path must contain {{yyyymmdd}} or {{yyyy-mm-dd}}: {rel}")
    parent_dir = root.joinpath(*parts[:idx]) if idx else root
    seg_prefix, _, seg_suffix = parts[idx].partition(token)
    remainder = parts[idx + 1:]
    date_re = r"(\d{8})" if token == "{yyyymmdd}" else r"(\d{4}-\d{2}-\d{2})"
    pattern = re.compile(rf"{re.escape(seg_prefix)}{date_re}{re.escape(seg_suffix)}$")
    candidates: list[tuple[str, Path]] = []
    if parent_dir.exists():
        for child in parent_dir.iterdir():
            m = pattern.match(child.name)
            if not m:
                continue
            target_file = child.joinpath(*remainder) if remainder else child
            if target_file.exists():
                candidates.append((m.group(1).replace("-", ""), target_file.resolve()))
    return sorted(candidates)


def _resolve_file(cfg: dict[str, Any], root: Path, run_as_of: str | None) -> Path:
    """Resolve a flat file or the dated-folder file for the run as-of (or latest <= as-of)."""
    mode = str(cfg.get("file_mode", "flat"))
    rel = str(cfg["file_path"])
    if mode == "flat":
        return (root / rel).resolve()
    if mode == "dated":
        candidates = dated_candidates(cfg, root)
        if not candidates:
            raise FileNotFoundError(f"No dated score file found for pattern {rel} under {root}")
        target = run_as_of.replace("-", "") if run_as_of else None
        eligible = [c for c in candidates if target is None or c[0] <= target]
        if target is not None and not eligible:
            earliest = min(c[0] for c in candidates)
            raise FileNotFoundError(
                f"No dated score file at or before {target} for pattern {rel} under {root}; "
                f"earliest available is {earliest}"
            )
        chosen = max(eligible, key=lambda c: c[0])
        return chosen[1]
    raise ValueError(f"Unknown file_mode: {mode}")


def _adapt_final_rank_family(
    cfg: dict[str, Any],
    rows: list[dict[str, str]],
    *,
    enforce_candidate_status: bool,
) -> list[CanonicalScore]:
    out: list[CanonicalScore] = []
    require_oos_score_valid = bool(cfg.get("require_oos_score_valid", False))
    lineage_policy = policy_for_model_family(str(cfg.get("model_family") or ""))
    lineage_policy_mode = str(
        cfg.get("_financial_lineage_policy_mode")
        or lineage_policy.mode_for("production")
    )
    require_financial_lineage = lineage_policy_mode != POLICY_DISABLED
    skipped = 0
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("final_score"))
        if not ticker or native is None:
            skipped += 1
            continue
        rank_ready = _truthy(r.get("rank_ready_flag"))
        production_candidate = _truthy(r.get("portfolio_candidate_gate")) or rank_ready
        lineage_required_for_row = require_financial_lineage and (
            lineage_policy_mode == POLICY_STRICT_UNIVERSE
            or (
                lineage_policy_mode == POLICY_CANDIDATE_ONLY
                and production_candidate
            )
        )
        missing_lineage_fields = [
            field for field in FINANCIAL_LINEAGE_FIELDS if field not in r
        ]
        if lineage_required_for_row and missing_lineage_fields:
            raise ValueError(
                f"{cfg.get('model_family')} row ticker={ticker} is missing financial lineage fields: "
                f"{missing_lineage_fields}"
            )
        lineage_status = str(r.get("financial_lineage_status") or "").strip()
        lineage_gate = _truthy(r.get("financial_lineage_gate"))
        lineage_checked = str(r.get("financial_lineage_checked_asof_date") or "").strip()
        lineage_ok = not lineage_required_for_row or lineage_row_is_safe(
            r,
            expected_asof=_source_asof(r),
            min_core_metric_count=lineage_policy.min_core_metric_count,
        )
        calib_ok = _truthy(r.get("calibration_eligible_flag"))
        complete = str(r.get("model_status", "")).strip().lower() == "complete"
        oos_score_valid = _oos_score_valid(r, cfg, default_requires_oos=require_oos_score_valid)
        status_denied = False
        if "portfolio_candidate_gate" in r:
            candidate_gate = _truthy(r.get("portfolio_candidate_gate"))
            candidate_status = str(r.get("portfolio_candidate_status") or "").strip().lower()
            status_allows = not enforce_candidate_status or candidate_status in {"", "eligible"}
            status_denied = bool(candidate_gate and not status_allows)
            eligible = candidate_gate and status_allows
        else:
            eligible = rank_ready and calib_ok and complete and (oos_score_valid or not require_oos_score_valid)
        demoted_not_oos = bool(eligible and not oos_score_valid)
        if eligible and not oos_score_valid:
            eligible = False
        demoted_financial_lineage = bool(eligible and not lineage_ok)
        if eligible and not lineage_ok:
            eligible = False
        source_reason = str(r.get("portfolio_candidate_reason") or "").strip()
        reason = (
            "ok" if eligible else
            f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}" if demoted_not_oos else
            f"financial_lineage:{lineage_status or 'missing'}" if demoted_financial_lineage else
            source_reason if source_reason and source_reason.lower() != "ok" else
            f"portfolio_candidate_status:{str(r.get('portfolio_candidate_status') or '').strip()[:80]}"
            if status_denied else
            "not_rank_ready" if not rank_ready else
            "not_calibration_eligible" if not calib_ok else
            "model_incomplete" if not complete else
            "not_oos_score_valid"
        )
        raw_research_eligible = _truthy(
            r.get("stage11_calibration_input_eligible_flag")
            if "stage11_calibration_input_eligible_flag" in r
            else r.get("research_calibration_input_eligible_flag")
        )
        source_role = _sample_role(r, investable=eligible, research_eligible=raw_research_eligible)
        if "stage11_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("stage11_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(
                r.get("stage11_calibration_input_reason")
                or r.get("research_calibration_reason")
                or r.get("calibration_status_reason")
                or ""
            ).strip()
            if eligible:
                research_eligible = True
                research_reason = "ok"
            if not research_reason:
                research_reason = _research_reason(
                    eligible=research_eligible,
                    reason=str(r.get("research_calibration_status") or "not_calibration_research_eligible"),
                )
            if raw_research_eligible and not research_eligible:
                research_reason = _research_guard_reason(r, source_role=source_role, oos_score_valid=oos_score_valid)
        elif "research_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("research_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(r.get("research_calibration_reason") or r.get("calibration_status_reason") or "").strip()
            if not research_reason:
                research_reason = _research_reason(
                    eligible=research_eligible,
                    reason=str(r.get("research_calibration_status") or "not_calibration_research_eligible"),
                )
            if raw_research_eligible and not research_eligible:
                research_reason = _research_guard_reason(r, source_role=source_role, oos_score_valid=oos_score_valid)
            if eligible:
                # investable implies research-eligible (contract invariant); reason must match
                research_eligible = True
                research_reason = "ok"
        else:
            research_eligible = calib_ok and complete and (oos_score_valid or not require_oos_score_valid)
            research_reason = _research_reason(
                eligible=research_eligible,
                reason=(
                    "not_calibration_eligible" if not calib_ok else
                    "model_incomplete" if not complete else
                    f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}"
                ),
            )
            if eligible:
                research_eligible = True
                research_reason = "ok"
        if lineage_required_for_row and not lineage_ok:
            research_eligible = False
            research_reason = f"financial_lineage:{lineage_status or 'missing'}"
            source_role = "excluded"
        contract_sample_role = _contract_sample_role(
            source_role,
            investable=eligible,
            research_eligible=research_eligible,
            oos_score_valid=oos_score_valid,
        )
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=str(cfg["industry"]),
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            calibration_research_eligible=int(research_eligible),
            calibration_research_reason=research_reason,
            calibration_sample_role=source_role,
            stage1_sample_role=contract_sample_role,
            oos_score_valid_flag=int(oos_score_valid),
            missing_score_flag=0,
            survivorship_corrected_panel_flag=int(_survivorship_corrected(r)),
            score_confidence=_confidence(
                r.get("score_confidence")
                if str(r.get("score_confidence", "")).strip()
                else r.get("data_quality_confidence")
            ),
            source_asof_date=_source_asof(r),
            model_scope_id=(
                str(r.get("calibration_cohort") or "").strip()
                if str(cfg.get("model_family") or "") == "consumer_defensive"
                else str(r.get("model_scope_id") or "").strip()
            ),
            production_policy_id=str(
                r.get("consumer_defensive_production_lock_id") or ""
            ).strip(),
            production_policy_sha256=str(
                r.get("consumer_defensive_production_lock_sha256") or ""
            ).strip().lower(),
            financial_lineage_checked_asof_date=lineage_checked,
            financial_lineage_status=lineage_status,
            financial_lineage_gate=int(lineage_gate),
            financial_lineage_classification=str(r.get("financial_lineage_classification") or ""),
            latest_material_financial_filing_date=str(r.get("latest_material_financial_filing_date") or ""),
            latest_material_financial_form=str(r.get("latest_material_financial_form") or ""),
            latest_material_financial_accession=str(r.get("latest_material_financial_accession") or ""),
            latest_material_financial_report_date=str(r.get("latest_material_financial_report_date") or ""),
            incorporated_financial_filing_date=str(r.get("incorporated_financial_filing_date") or ""),
            incorporated_financial_accession=str(r.get("incorporated_financial_accession") or ""),
            incorporated_financial_report_date=str(r.get("incorporated_financial_report_date") or ""),
            incorporated_financial_core_metric_count=int(_f(r.get("incorporated_financial_core_metric_count")) or 0),
            financial_lineage_reason=str(r.get("financial_lineage_reason") or ""),
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "tech_family"), skipped)
    return out


def _adapt_tech_family(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    return _adapt_final_rank_family(cfg, rows, enforce_candidate_status=False)


def _adapt_industrial_family(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    for idx, row in enumerate(rows, start=1):
        missing = sorted(set(INDUSTRIAL_FAMILY_REQUIRED_COLUMNS) - set(row))
        missing_groups = [
            "/".join(group)
            for group in INDUSTRIAL_FAMILY_ONE_OF_COLUMNS
            if not any(column in row for column in group)
        ]
        if missing:
            raise ValueError(
                f"industrial_family score file for {cfg.get('model_family')} row {idx} "
                f"ticker={str(row.get('ticker') or '').strip() or '<blank>'} "
                f"is missing required columns: {missing}"
            )
        if missing_groups:
            raise ValueError(
                f"industrial_family score file for {cfg.get('model_family')} row {idx} "
                f"ticker={str(row.get('ticker') or '').strip() or '<blank>'} "
                f"must include one column from each group: {missing_groups}"
            )
    return _adapt_final_rank_family(cfg, rows, enforce_candidate_status=True)


def _transportation_iso(
    value: object,
    *,
    label: str,
    required: bool,
) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        if required:
            raise ValueError(f"{label} is required")
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO date") from exc


def _require_zero_transportation_shadow_alpha(cfg: dict[str, Any]) -> None:
    calibration = cfg.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError(
            "shadow Transportation source requires an explicit zero-alpha calibration"
        )
    alpha = _f(calibration.get("expected_alpha_at_full"))
    if alpha is None or not math.isclose(alpha, 0.0, abs_tol=1e-12):
        raise ValueError(
            "shadow Transportation source requires expected_alpha_at_full=0"
        )


def _require_zero_consumer_defensive_shadow_alpha(
    cfg: dict[str, Any],
) -> None:
    calibration = cfg.get("calibration")
    if not isinstance(calibration, dict):
        raise ValueError(
            "shadow Consumer Defensive source requires an explicit "
            "zero-alpha calibration"
        )
    alpha = _f(calibration.get("expected_alpha_at_full"))
    if alpha is None or not math.isclose(alpha, 0.0, abs_tol=1e-12):
        raise ValueError(
            "shadow Consumer Defensive source requires "
            "expected_alpha_at_full=0"
        )


def _adapt_transportation_subgroup(
    cfg: dict[str, Any],
    rows: list[dict[str, str]],
) -> list[CanonicalScore]:
    """Consume only complete, unambiguous Transportation subgroup lineage."""
    if str(cfg.get("model_family") or "") != "transportation":
        raise ValueError(
            "transportation_subgroup adapter is reserved for transportation"
        )
    if not rows:
        return []
    scoring_modes = {
        str(row.get("transportation_scoring_mode") or "").strip()
        for row in rows
    }
    if scoring_modes == {""}:
        # Preserve read-only diagnostics for immutable pre-v8 shadow files.
        # They can never assert OOS/candidate gates through this compatibility
        # branch and the configured Transportation alpha/cap remain zero.
        for row in rows:
            ticker = str(row.get("ticker") or "").strip().upper() or "<blank>"
            if (
                _truthy(row.get("portfolio_candidate_gate"))
                or _truthy(row.get("oos_score_valid_flag"))
                or str(row.get("portfolio_candidate_status") or "")
                != "shadow_only"
            ):
                raise ValueError(
                    f"{ticker}: legacy Transportation row is not shadow-only"
                )
            if (
                _truthy(row.get("research_calibration_input_eligible_flag"))
                or _truthy(row.get("stage11_calibration_input_eligible_flag"))
                or _truthy(row.get("survivorship_corrected_panel_flag"))
            ):
                raise ValueError(
                    f"{ticker}: legacy Transportation shadow row cannot assert research lineage"
                )
        _require_zero_transportation_shadow_alpha(cfg)
        return _adapt_industrial_family(cfg, rows)
    if scoring_modes != {"subgroup_v8"}:
        raise ValueError(
            "transportation_subgroup file mixes subgroup and legacy scoring modes"
        )
    tickers: set[str] = set()
    group_recipe_hashes: dict[str, set[str]] = defaultdict(set)
    group_modes: dict[str, set[str]] = defaultdict(set)
    group_weights: dict[str, set[float]] = defaultdict(set)
    group_expected_counts: dict[str, set[int]] = defaultdict(set)
    group_ranks: dict[str, list[int]] = defaultdict(list)
    group_cohorts: dict[str, str] = {}
    recipe_versions: set[str] = set()
    policy_hashes: set[str] = set()
    states: set[str] = set()
    production_lock_ids: set[str] = set()
    decision_hashes: set[str] = set()
    expected_group_counts: set[int] = set()
    expected_ticker_counts: set[int] = set()
    for idx, row in enumerate(rows, start=1):
        ticker = str(row.get("ticker") or "").strip().upper() or "<blank>"
        missing = sorted(
            set(TRANSPORTATION_SUBGROUP_REQUIRED_COLUMNS) - set(row)
        )
        if missing:
            raise ValueError(
                "transportation_subgroup score file row "
                f"{idx} ticker={ticker} is missing required columns: {missing}"
            )
        if ticker in tickers:
            raise ValueError(
                f"transportation_subgroup has duplicate/ambiguous ticker={ticker}"
            )
        tickers.add(ticker)
        if str(row.get("sector") or "").strip() != "Industrials":
            raise ValueError(
                f"{ticker}: Transportation row must use sector=Industrials"
            )
        if str(row.get("industry_aggregate") or "").strip() != "Transportation":
            raise ValueError(
                f"{ticker}: Transportation row has wrong industry_aggregate"
            )
        if str(row.get("transportation_scoring_mode") or "") != "subgroup_v8":
            raise ValueError(
                f"{ticker}: unsupported transportation_scoring_mode"
            )
        state = str(row.get("transportation_production_state") or "")
        if state not in TRANSPORTATION_SUBGROUP_STATES:
            raise ValueError(
                f"{ticker}: invalid transportation_production_state={state!r}"
            )
        states.add(state)
        for field in TRANSPORTATION_SUBGROUP_SHA_FIELDS:
            value = str(row.get(field) or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{ticker}: invalid {field}")
        cohort_id = str(row.get("transportation_cohort_id") or "").strip()
        group_id = str(row.get("transportation_group_id") or "").strip()
        recipe_key = str(
            row.get("transportation_group_recipe_key") or ""
        ).strip()
        if not cohort_id or not group_id or recipe_key != f"{cohort_id}::{group_id}":
            raise ValueError(f"{ticker}: subgroup recipe identity is inconsistent")
        ranking_mode = str(
            row.get("transportation_group_ranking_mode") or ""
        )
        if ranking_mode not in {"ranked", "eligibility_equal_weight"}:
            raise ValueError(f"{ticker}: invalid subgroup ranking mode")
        recipe_state = str(
            row.get("transportation_component_recipe_state") or ""
        )
        if recipe_state not in {"active", "fallback"}:
            raise ValueError(f"{ticker}: invalid component recipe state")
        membership_scope = str(
            row.get("transportation_membership_scope") or ""
        )
        if membership_scope not in {
            "current_recipe",
            "historical_calibration_only",
            "pre_effective_policy_diagnostic_replay",
        }:
            raise ValueError(f"{ticker}: invalid subgroup membership scope")
        asof = _transportation_iso(
            row.get("asof_date"),
            label=f"{ticker}.asof_date",
            required=True,
        )
        assert asof is not None
        start = _transportation_iso(
            row.get("transportation_membership_effective_from"),
            label=f"{ticker}.transportation_membership_effective_from",
            required=False,
        )
        end = _transportation_iso(
            row.get("transportation_membership_effective_to"),
            label=f"{ticker}.transportation_membership_effective_to",
            required=False,
        )
        if (start is not None and start > asof) or (
            end is not None and end < asof
        ):
            raise ValueError(
                f"{ticker}: subgroup membership does not cover score date"
            )
        if start is not None and end is not None and start > end:
            raise ValueError(f"{ticker}: subgroup membership range is reversed")
        if membership_scope in {
            "historical_calibration_only",
            "pre_effective_policy_diagnostic_replay",
        } and (start is None or end is None):
            raise ValueError(
                f"{ticker}: historical subgroup membership must be bounded"
            )
        if membership_scope == "current_recipe" and start is None:
            raise ValueError(
                f"{ticker}: current subgroup membership must have an effective start"
            )
        if state == "shadow":
            if _truthy(row.get("portfolio_candidate_gate")) or _truthy(
                row.get("oos_score_valid_flag")
            ):
                raise ValueError(
                    f"{ticker}: shadow subgroup row cannot assert portfolio/OOS gates"
                )
            if str(row.get("portfolio_candidate_status") or "") != "shadow_only":
                raise ValueError(
                    f"{ticker}: shadow subgroup row must use status=shadow_only"
                )
            if _truthy(
                row.get("research_calibration_input_eligible_flag")
            ) or _truthy(row.get("stage11_calibration_input_eligible_flag")) or _truthy(
                row.get("survivorship_corrected_panel_flag")
            ):
                raise ValueError(
                    f"{ticker}: shadow subgroup row cannot assert research gates"
                )
            if str(row.get("transportation_production_lock_id") or "").strip():
                raise ValueError(
                    f"{ticker}: shadow subgroup row cannot assert a production lock"
                )
            if str(
                row.get("transportation_decision_manifest_sha256") or ""
            ).strip():
                raise ValueError(
                    f"{ticker}: shadow subgroup row cannot assert decision lineage"
                )
        else:
            lock_id = str(
                row.get("transportation_production_lock_id") or ""
            ).strip()
            decision_sha = str(
                row.get("transportation_decision_manifest_sha256") or ""
            ).strip().lower()
            if not lock_id or not re.fullmatch(r"[0-9a-f]{64}", decision_sha):
                raise ValueError(
                    f"{ticker}: promoted subgroup row lacks lock/decision lineage"
                )
            production_lock_ids.add(lock_id)
            decision_hashes.add(decision_sha)
        weight = _f(row.get("transportation_group_aggregate_weight"))
        if weight is None or not 0.0 < weight <= 1.0:
            raise ValueError(f"{ticker}: invalid group aggregate weight")
        try:
            expected_groups = int(
                str(row.get("transportation_expected_group_count") or "")
            )
            expected_tickers = int(
                str(row.get("transportation_expected_ticker_count") or "")
            )
            expected_in_group = int(
                str(
                    row.get(
                        "transportation_group_expected_ticker_count"
                    )
                    or ""
                )
            )
            group_rank = int(
                str(row.get("transportation_group_rank") or "")
            )
        except ValueError as exc:
            raise ValueError(
                f"{ticker}: invalid subgroup census/rank fields"
            ) from exc
        if min(
            expected_groups,
            expected_tickers,
            expected_in_group,
            group_rank,
        ) <= 0:
            raise ValueError(f"{ticker}: subgroup census/rank must be positive")
        recipe_versions.add(
            str(row.get("transportation_group_recipe_version") or "")
        )
        policy_hashes.add(
            str(row.get("transportation_subgroup_policy_sha256") or "")
        )
        expected_group_counts.add(expected_groups)
        expected_ticker_counts.add(expected_tickers)
        group_recipe_hashes[recipe_key].add(
            str(row.get("transportation_group_recipe_sha256") or "")
        )
        group_modes[recipe_key].add(ranking_mode)
        group_weights[recipe_key].add(weight)
        group_expected_counts[recipe_key].add(expected_in_group)
        group_ranks[recipe_key].append(group_rank)
        group_cohorts[recipe_key] = cohort_id

    if any(
        len(values) != 1
        for values in (
            recipe_versions,
            policy_hashes,
            states,
            expected_group_counts,
            expected_ticker_counts,
        )
    ):
        raise ValueError(
            "transportation_subgroup file has mixed lock/policy/state lineage"
        )
    if not next(iter(recipe_versions)).strip():
        raise ValueError("transportation_subgroup recipe version is blank")
    state = next(iter(states))
    if state == "shadow":
        _require_zero_transportation_shadow_alpha(cfg)
    if state == "promoted" and (
        len(production_lock_ids) != 1 or len(decision_hashes) != 1
    ):
        raise ValueError(
            "transportation_subgroup promoted rows have mixed lock lineage"
        )
    expected_group_count = next(iter(expected_group_counts))
    expected_ticker_count = next(iter(expected_ticker_counts))
    if len(group_recipe_hashes) != expected_group_count:
        raise ValueError(
            "transportation_subgroup file is missing or has extra group recipes"
        )
    if len(tickers) != expected_ticker_count:
        raise ValueError(
            "transportation_subgroup ticker census does not tie"
        )
    for recipe_key in group_recipe_hashes:
        if (
            len(group_recipe_hashes[recipe_key]) != 1
            or len(group_modes[recipe_key]) != 1
            or len(group_weights[recipe_key]) != 1
            or len(group_expected_counts[recipe_key]) != 1
        ):
            raise ValueError(
                f"{recipe_key}: ambiguous subgroup recipe lineage"
            )
        expected = next(iter(group_expected_counts[recipe_key]))
        if len(group_ranks[recipe_key]) != expected:
            raise ValueError(
                f"{recipe_key}: subgroup ticker census does not tie"
            )
        if sorted(group_ranks[recipe_key]) != list(
            range(1, expected + 1)
        ):
            raise ValueError(f"{recipe_key}: subgroup ranks are not contiguous")
    cohort_weight_sums: dict[str, float] = defaultdict(float)
    for recipe_key, values in group_weights.items():
        cohort_weight_sums[group_cohorts[recipe_key]] += next(iter(values))
    for cohort_id, total in cohort_weight_sums.items():
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            raise ValueError(
                f"{cohort_id}: locked subgroup aggregate weights do not sum to one"
            )
    if state == "promoted":
        raise ValueError(
            "promoted Transportation subgroup consumption is disabled until "
            "hash-bound lock/decision verification and fixed-group portfolio "
            "aggregation are implemented"
        )
    return _adapt_final_rank_family(
        cfg,
        rows,
        enforce_candidate_status=True,
    )


def _consumer_v3_sha(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _consumer_v3_date(value: object, *, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} must be a canonical ISO date")
    return parsed


def _consumer_v3_canonical_sha256(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "payload_sha256"}
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _consumer_v3_file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _consumer_v3_read_csv_snapshot(
    path: Path,
) -> tuple[list[dict[str, str]], tuple[str, ...], str]:
    """Read and hash one immutable byte snapshot to avoid path re-read races."""

    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = tuple(reader.fieldnames or ())
    if (
        not fieldnames
        or any(not field.strip() for field in fieldnames)
        or len(fieldnames) != len(set(fieldnames))
    ):
        raise ValueError(
            "Consumer Defensive score CSV has blank or duplicate headers"
        )
    return list(reader), fieldnames, hashlib.sha256(raw).hexdigest()


def _consumer_v3_value_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _consumer_v3_rank_row_sha256(row: Mapping[str, Any]) -> str:
    expected = set(CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS) - {"row_sha256"}
    observed = set(row) - {"row_sha256"}
    if observed != expected:
        raise ValueError("Consumer Defensive rank-row schema drifted")
    body = {
        field: "" if row[field] is None else str(row[field])
        for field in CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS
        if field != "row_sha256"
    }
    return _consumer_v3_canonical_sha256(body)


def _consumer_v3_strict_json_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], str]:
    """Parse and hash one JSON byte snapshot without re-reading its path."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} duplicates key={key!r}")
            result[key] = value
        return result

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
        payload = json.loads(
            text,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(
                    f"{label} rejects non-finite JSON constant {value}"
                )
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload, hashlib.sha256(raw).hexdigest()


def _validate_consumer_calibration_scope(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != (
        CONSUMER_DEFENSIVE_CALIBRATION_SCOPE_KEYS
    ):
        raise ValueError("Consumer calibration-scope contract has the wrong schema")
    scope = dict(payload)
    if scope["mode"] != "explicit_ticker_exclusions" or scope[
        "enforcement_stage"
    ] != "before_cross_section_normalization":
        raise ValueError("Consumer calibration-scope enforcement policy changed")
    cohorts = scope["excluded_tickers_by_cohort"]
    expected_counts = scope["expected_remaining_current_by_cohort"]
    if (
        not isinstance(cohorts, dict)
        or not isinstance(expected_counts, dict)
        or set(cohorts) != CONSUMER_DEFENSIVE_COHORTS
        or set(expected_counts) != CONSUMER_DEFENSIVE_COHORTS
    ):
        raise ValueError("Consumer calibration-scope cohort census changed")
    excluded: list[str] = []
    for cohort in sorted(CONSUMER_DEFENSIVE_COHORTS):
        values = cohorts[cohort]
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value.strip() for value in values)
            or values != sorted(set(values))
        ):
            raise ValueError(f"Consumer calibration exclusions for {cohort} are invalid")
        excluded.extend(values)
        count = expected_counts[cohort]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f"Consumer calibration-scope count for {cohort} is invalid")
    if len(excluded) != len(set(excluded)):
        raise ValueError("Consumer calibration exclusions overlap across cohorts")
    if scope["excluded_tickers"] != sorted(excluded) or int(
        scope["excluded_ticker_count"]
    ) != len(excluded):
        raise ValueError("Consumer calibration exclusion census does not tie")
    if int(scope["expected_remaining_current_ticker_count"]) != sum(
        int(value) for value in expected_counts.values()
    ):
        raise ValueError("Consumer remaining ticker census does not tie")
    remaining_ticker_sha = _consumer_v3_sha(
        scope["expected_remaining_current_tickers_sha256"],
        label="Consumer expected remaining ticker-set SHA",
    )
    scope["expected_remaining_current_tickers_sha256"] = remaining_ticker_sha
    observed_sha = _consumer_v3_canonical_sha256(scope)
    if observed_sha != _consumer_v3_sha(
        scope["payload_sha256"], label="Consumer calibration-scope payload hash"
    ):
        raise ValueError("Consumer calibration-scope self-hash mismatch")
    return scope


def _load_consumer_v3_score_manifest(
    cfg: dict[str, Any],
    source_file: Path,
    rows: list[dict[str, str]],
    *,
    source_asof: str,
    source_file_sha256: str,
) -> tuple[dict[str, Any], Path, dict[str, Any], str]:
    filename = str(cfg.get("production_score_manifest_filename") or "").strip()
    configured_scope_sha = str(
        cfg.get("production_calibration_scope_sha256") or ""
    ).strip()
    if not filename or Path(filename).name != filename:
        raise ValueError("Consumer production score-manifest filename is invalid")
    scope_pin = _consumer_v3_sha(
        configured_scope_sha,
        label="configured Consumer calibration-scope SHA",
    )
    manifest_path = source_file.with_name(filename).resolve()
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError(f"Consumer production score manifest is missing or unsafe: {manifest_path}")
    manifest, manifest_file_sha256 = _consumer_v3_strict_json_snapshot(
        manifest_path,
        label="Consumer production score manifest",
    )
    if (
        manifest.get("schema_version")
        != "consumer_defensive_production_score_manifest_v3"
        or str(manifest.get("status") or "").upper() != "PASS"
    ):
        raise ValueError("Consumer production score manifest is not a v3 PASS artifact")
    unsigned = dict(manifest)
    payload_sha = str(unsigned.pop("payload_sha256", ""))
    if payload_sha != _consumer_v3_canonical_sha256(unsigned):
        raise ValueError("Consumer production score manifest self-hash mismatch")
    if str(manifest.get("allocation_asof_date") or "") != source_asof:
        raise ValueError("Consumer production score manifest date mismatch")
    declared_rank = Path(str(manifest.get("rank_csv_path") or "")).resolve()
    if declared_rank != source_file.resolve() or str(
        manifest.get("rank_csv_file_sha256") or ""
    ) != source_file_sha256:
        raise ValueError("Consumer production score manifest/rank binding failed")
    scope = _validate_consumer_calibration_scope(
        manifest.get("calibration_scope_contract")
    )
    if scope["payload_sha256"] != scope_pin or str(
        manifest.get("calibration_scope_sha256") or ""
    ) != scope_pin:
        raise ValueError("Consumer calibration scope does not match the Portfolio pin")
    tickers = [str(row.get("ticker") or "").strip().upper() for row in rows]
    if any(
        str(row.get("row_sha256") or "")
        != _consumer_v3_rank_row_sha256(row)
        for row in rows
    ):
        raise ValueError("Consumer production score row self-hash mismatch")
    cohort_counts = {
        cohort: sum(
            str(row.get("calibration_cohort") or "").strip() == cohort
            for row in rows
        )
        for cohort in sorted(CONSUMER_DEFENSIVE_COHORTS)
    }
    if (
        len(tickers) != len(set(tickers))
        or int(manifest.get("rank_row_count", -1)) != len(rows)
        or int(manifest.get("published_ticker_count", -1)) != len(rows)
        or str(manifest.get("published_tickers_sha256") or "")
        != _consumer_v3_value_sha256(sorted(tickers))
        or str(manifest.get("published_tickers_sha256") or "")
        != str(scope["expected_remaining_current_tickers_sha256"])
        or manifest.get("published_tickers_by_cohort") != cohort_counts
    ):
        raise ValueError("Consumer production score-manifest ticker census drifted")
    return scope, manifest_path, manifest, manifest_file_sha256


def _resolve_consumer_v3_terminal_manifest(
    cfg: Mapping[str, Any],
    root: Path,
    *,
    source_asof: str,
) -> Path:
    template = str(
        cfg.get("production_terminal_manifest_file_path") or ""
    ).strip()
    if "{yyyy-mm-dd}" in template:
        relative = template.replace("{yyyy-mm-dd}", source_asof)
    elif "{yyyymmdd}" in template:
        relative = template.replace(
            "{yyyymmdd}", source_asof.replace("-", "")
        )
    else:
        raise ValueError(
            "Consumer production terminal-manifest path must contain "
            "{yyyy-mm-dd} or {yyyymmdd}"
        )
    base = root.expanduser().resolve()
    unresolved = Path(relative).expanduser()
    if unresolved.is_absolute():
        raise ValueError(
            "Consumer production terminal-manifest path must be relative"
        )
    path = (base / unresolved).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(
            "Consumer production terminal manifest escapes sector_output_root"
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(
            "Consumer production terminal manifest is missing or unsafe: "
            f"{path}"
        )
    return path


def _load_consumer_v3_terminal_manifest(
    cfg: Mapping[str, Any],
    root: Path,
    *,
    source_asof: str,
    source_file: Path,
    source_file_sha256: str,
    source_row_count: int,
    score_manifest_path: Path,
    score_manifest: Mapping[str, Any],
    score_manifest_file_sha256: str,
    calibration_scope_sha256: str,
) -> Path:
    """Require the same-date terminal PASS that sealed parsed score bytes."""

    terminal_path = _resolve_consumer_v3_terminal_manifest(
        cfg,
        root,
        source_asof=source_asof,
    )
    terminal, _terminal_file_sha256 = _consumer_v3_strict_json_snapshot(
        terminal_path,
        label="Consumer production terminal manifest",
    )
    if (
        terminal.get("schema_version")
        != "consumer_defensive_production_refresh_manifest_v3"
        or str(terminal.get("status") or "").upper() != "PASS"
    ):
        raise ValueError(
            "Consumer production terminal manifest is not a v3 PASS artifact"
        )
    if (
        str(terminal.get("asof_date") or "") != source_asof
        or str(terminal.get("signal_asof_date") or "")
        != str(score_manifest.get("signal_asof_date") or "")
    ):
        raise ValueError("Consumer production terminal manifest date mismatch")
    if terminal.get("failure") not in (None, ""):
        raise ValueError("Consumer production terminal PASS contains a failure")

    artifacts = terminal.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "rank_table",
        "publisher_manifest",
    }:
        raise ValueError(
            "Consumer production terminal artifact census is invalid"
        )
    rank = artifacts["rank_table"]
    publisher = artifacts["publisher_manifest"]
    if not isinstance(rank, dict) or not isinstance(publisher, dict):
        raise ValueError(
            "Consumer production terminal artifact records are invalid"
        )
    if (
        Path(str(rank.get("path") or "")).resolve()
        != source_file.resolve()
        or str(rank.get("sha256") or "") != source_file_sha256
        or int(rank.get("rows", -1)) != source_row_count
        or str(rank.get("calibration_scope_sha256") or "")
        != calibration_scope_sha256
    ):
        raise ValueError(
            "Consumer production terminal/rank byte binding failed"
        )
    if (
        Path(str(publisher.get("path") or "")).resolve()
        != score_manifest_path.resolve()
        or str(publisher.get("sha256") or "")
        != score_manifest_file_sha256
        or str(publisher.get("status") or "").upper() != "PASS"
    ):
        raise ValueError(
            "Consumer production terminal/publisher byte binding failed"
        )

    steps = terminal.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Consumer production terminal has no completed steps")
    for expected_sequence, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(
                "Consumer production terminal contains an invalid step"
            )
        sequence = step.get("sequence")
        return_code = step.get("return_code")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != expected_sequence
            or str(step.get("status") or "").upper() != "PASS"
            or isinstance(return_code, bool)
            or not isinstance(return_code, int)
            or return_code != 0
        ):
            raise ValueError(
                "Consumer production terminal contains a non-PASS step"
            )
    return terminal_path


def _consumer_v3_finite(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be finite numeric evidence")
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be finite numeric evidence") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite numeric evidence")
    return parsed


def _validate_consumer_v3_activation_registry(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != CONSUMER_DEFENSIVE_V3_REGISTRY_KEYS:
        raise ValueError("Consumer Defensive activation registry has the wrong root schema")
    registry = dict(payload)
    if (
        registry["schema_version"]
        != "consumer_defensive_production_activation_registry_v3"
        or registry["model_family"] != "consumer_defensive"
    ):
        raise ValueError("unsupported Consumer Defensive activation registry")
    registry_asof = _consumer_v3_date(
        registry["asof_date"], label="Consumer registry asof_date"
    )
    effective = _consumer_v3_date(
        registry["effective_from"], label="Consumer registry effective_from"
    )
    expiry = _consumer_v3_date(
        registry["valid_until"], label="Consumer registry valid_until"
    )
    maximum_age = registry["maximum_activation_age_days"]
    if isinstance(maximum_age, bool) or not isinstance(maximum_age, int) or maximum_age != 63:
        raise ValueError("Consumer registry maximum activation age must be 63 days")
    if not registry_asof <= effective <= expiry:
        raise ValueError("Consumer registry dates violate the v3 activation policy")
    authority_deadline = registry_asof + timedelta(days=maximum_age)
    if effective > authority_deadline or expiry > authority_deadline:
        raise ValueError(
            "Consumer registry dates exceed the decision-anchored authority window"
        )
    for field in ("decision_sha256", "framework_sha256", "source_input_sha256"):
        _consumer_v3_sha(registry[field], label=f"Consumer registry {field}")
    if type(registry["calibration_write_performed"]) is not bool or type(
        registry["portfolio_write_performed"]
    ) is not bool:
        raise ValueError("Consumer registry write flags must be booleans")
    if registry["calibration_write_performed"] or registry["portfolio_write_performed"]:
        raise ValueError("Consumer registry cannot claim calibration or Portfolio writes")
    cohorts = registry["cohorts"]
    if not isinstance(cohorts, dict) or set(cohorts) != CONSUMER_DEFENSIVE_COHORTS:
        raise ValueError("Consumer registry cohort census changed")
    normalized: dict[str, Any] = {}
    approved_full_caps: dict[str, float] = {}
    for cohort in sorted(CONSUMER_DEFENSIVE_COHORTS):
        raw = cohorts[cohort]
        if not isinstance(raw, dict) or set(raw) != CONSUMER_DEFENSIVE_V3_LOCK_KEYS:
            raise ValueError(f"Consumer activation lock {cohort} has the wrong schema")
        lock = dict(raw)
        if (
            lock["schema_version"] != "consumer_defensive_activation_lock_v3"
            or lock["model_family"] != "consumer_defensive"
            or lock["cohort"] != cohort
        ):
            raise ValueError(f"Consumer activation lock {cohort} identity mismatch")
        state = str(lock["deployment_state"])
        if state not in CONSUMER_DEFENSIVE_V3_ALLOWED_STATES:
            raise ValueError(f"Consumer activation lock {cohort} has an invalid state")
        if type(lock["investable"]) is not bool:
            raise ValueError(f"Consumer activation lock {cohort} investable must be boolean")
        lock_effective = _consumer_v3_date(
            lock["effective_from"], label=f"Consumer lock {cohort} effective_from"
        )
        lock_expiry = _consumer_v3_date(
            lock["valid_until"], label=f"Consumer lock {cohort} valid_until"
        )
        if lock_effective != effective or lock_expiry != expiry:
            raise ValueError(f"Consumer activation lock {cohort} dates are not registry-bound")
        for field in (
            "decision_sha256", "framework_sha256", "source_input_sha256",
            "model_contract_sha256", "payload_sha256",
        ):
            _consumer_v3_sha(lock[field], label=f"Consumer lock {cohort}.{field}")
        if (
            lock["decision_sha256"] != registry["decision_sha256"]
            or lock["framework_sha256"] != registry["framework_sha256"]
            or lock["source_input_sha256"] != registry["source_input_sha256"]
        ):
            raise ValueError(f"Consumer activation lock {cohort} lineage mismatch")
        expected_lock_id = "cdv3_" + hashlib.sha256(
            json.dumps(
                {
                    "cohort": cohort,
                    "decision_sha256": lock["decision_sha256"],
                    "effective_from": lock["effective_from"],
                    "model_contract_sha256": lock["model_contract_sha256"],
                    "valid_until": lock["valid_until"],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()[:24]
        if lock["lock_id"] != expected_lock_id:
            raise ValueError(f"Consumer activation lock {cohort} id is not reproducible")
        numbers = {
            field: _consumer_v3_finite(lock[field], label=f"Consumer lock {cohort}.{field}")
            for field in (
                "tier_deployment_fraction", "effective_deployment_fraction",
                "approved_full_portfolio_cap", "optimizer_cap",
                "expected_alpha_at_full", "confidence_multiplier",
            )
        }
        if not all(0.0 <= numbers[field] <= 1.0 for field in (
            "tier_deployment_fraction", "effective_deployment_fraction",
            "approved_full_portfolio_cap", "optimizer_cap", "confidence_multiplier",
        )) or numbers["expected_alpha_at_full"] < 0.0:
            raise ValueError(f"Consumer activation lock {cohort} numeric bounds failed")
        if numbers["confidence_multiplier"] <= 0.0:
            raise ValueError(f"Consumer activation lock {cohort} confidence must be positive")
        if not math.isclose(
            numbers["optimizer_cap"],
            numbers["approved_full_portfolio_cap"]
            * numbers["effective_deployment_fraction"],
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(f"Consumer activation lock {cohort} cap arithmetic failed")
        if state == CONSUMER_DEFENSIVE_V3_STANDARD_STATE:
            if (
                lock["investable"] is not True
                or lock["promotion_state"] != "promoted"
                or numbers["approved_full_portfolio_cap"] <= 0.0
                or numbers["expected_alpha_at_full"] <= 0.0
                or not math.isclose(
                    numbers["tier_deployment_fraction"], 1.0, abs_tol=1e-12
                )
                or not math.isclose(
                    numbers["effective_deployment_fraction"], 1.0, abs_tol=1e-12
                )
                or not math.isclose(
                    numbers["optimizer_cap"],
                    numbers["approved_full_portfolio_cap"],
                    abs_tol=1e-12,
                )
            ):
                raise ValueError(
                    f"Consumer activation lock {cohort} is not a full standard allocation"
                )
        elif (
            lock["investable"] is not False
            or lock["promotion_state"] != "shadow_monitor"
            or not math.isclose(
                numbers["tier_deployment_fraction"], 0.0, abs_tol=1e-12
            )
            or not math.isclose(
                numbers["effective_deployment_fraction"], 0.0, abs_tol=1e-12
            )
            or not math.isclose(numbers["optimizer_cap"], 0.0, abs_tol=1e-12)
            or not math.isclose(
                numbers["expected_alpha_at_full"], 0.0, abs_tol=1e-12
            )
        ):
            raise ValueError(
                f"Consumer activation lock {cohort} non-promoted slot must remain cash"
            )
        for field in ("selected_candidate_id", "score_model_version", "scoring_contract_version"):
            if not isinstance(lock[field], str) or not lock[field].strip():
                raise ValueError(f"Consumer activation lock {cohort}.{field} is blank")
        if _consumer_v3_canonical_sha256(lock) != lock["payload_sha256"]:
            raise ValueError(f"Consumer activation lock {cohort} self-hash mismatch")
        approved_full_caps[cohort] = numbers["approved_full_portfolio_cap"]
        normalized[cohort] = lock
    first_cap = approved_full_caps[sorted(approved_full_caps)[0]]
    if first_cap <= 0.0 or any(
        not math.isclose(cap, first_cap, rel_tol=0.0, abs_tol=1e-12)
        for cap in approved_full_caps.values()
    ):
        raise ValueError("Consumer registry must reserve equal cohort allocation slots")
    if sum(approved_full_caps.values()) > 1.0 + 1e-12:
        raise ValueError("Consumer registry approved sector allocation exceeds gross")
    if _consumer_v3_canonical_sha256(registry) != _consumer_v3_sha(
        registry["payload_sha256"], label="Consumer registry payload hash"
    ):
        raise ValueError("Consumer activation registry self-hash mismatch")
    return {**registry, "cohorts": normalized}


def _load_consumer_v3_activation_registry(
    cfg: dict[str, Any],
    root: Path,
    *,
    source_asof: str,
    portfolio_asof: str,
) -> tuple[dict[str, dict[str, Any]], Path | None]:
    raw_path = str(cfg.get("production_activation_registry_file_path") or "").strip()
    configured_sha = str(cfg.get("production_activation_registry_sha256") or "").strip()
    if bool(raw_path) != bool(configured_sha):
        raise ValueError("Consumer activation registry path and SHA must be configured together")
    if str(cfg.get("production_change_control_public_key_path") or "").strip():
        raise ValueError("Consumer public-key configuration is unsupported until signature verification exists")
    if not raw_path:
        return {}, None
    base = root.expanduser().resolve()
    unresolved = Path(raw_path).expanduser()
    candidate = (base / unresolved).resolve() if not unresolved.is_absolute() else unresolved.resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("Consumer activation registry escapes sector_output_root") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise ValueError(f"Consumer activation registry is missing or unsafe: {candidate}")
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Consumer activation registry duplicates key={key!r}")
            result[key] = value
        return result
    payload = json.loads(
        candidate.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"Consumer registry rejects non-finite JSON constant {value}")
        ),
    )
    registry = _validate_consumer_v3_activation_registry(payload)
    if registry["payload_sha256"] != _consumer_v3_sha(
        configured_sha, label="configured Consumer activation registry SHA"
    ):
        raise ValueError("Consumer activation registry does not match the configured SHA pin")
    configured_caps = cfg.get("optimizer_cap_by_scope") or {}
    configured_calibrations = cfg.get("calibration_by_scope") or {}
    if not isinstance(configured_caps, Mapping) or not isinstance(
        configured_calibrations, Mapping
    ):
        raise ValueError("Consumer v3 per-cohort capital maps must be mappings")
    if (
        set(configured_caps) != CONSUMER_DEFENSIVE_COHORTS
        or set(configured_calibrations) != CONSUMER_DEFENSIVE_COHORTS
    ):
        raise ValueError("Consumer v3 authority requires the exact per-cohort census")
    for cohort, lock in registry["cohorts"].items():
        calibration = configured_calibrations.get(cohort)
        if not isinstance(calibration, Mapping):
            raise ValueError(f"Consumer calibration {cohort} must be a mapping")
        configured_cap = _consumer_v3_finite(
            configured_caps[cohort], label=f"Consumer configured cap {cohort}"
        )
        configured_alpha = _consumer_v3_finite(
            calibration.get("expected_alpha_at_full"), label=f"Consumer configured alpha {cohort}"
        )
        if not math.isclose(configured_cap, float(lock["optimizer_cap"]), abs_tol=1e-12):
            raise ValueError(f"Consumer configured cap {cohort} is not bound to registry authority")
        if not math.isclose(
            configured_alpha, float(lock["expected_alpha_at_full"]), abs_tol=1e-12
        ):
            raise ValueError(f"Consumer configured alpha {cohort} is not bound to registry authority")
    configured_sector_cap = _consumer_v3_finite(
        cfg.get("optimizer_sector_cap"),
        label="Consumer configured sector cap",
    )
    registry_sector_cap = sum(
        float(lock["approved_full_portfolio_cap"])
        for lock in registry["cohorts"].values()
    )
    if not math.isclose(
        configured_sector_cap,
        registry_sector_cap,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError(
            "Consumer configured sector cap is not bound to registry allocation slots"
        )
    effective = _consumer_v3_date(
        registry["effective_from"], label="registry effective_from"
    )
    expiry = _consumer_v3_date(registry["valid_until"], label="registry valid_until")
    source_date = _consumer_v3_date(source_asof, label="Consumer score source asof_date")
    if not effective <= source_date <= expiry:
        raise ValueError("Consumer score snapshot falls outside registry authority dates")
    portfolio_date = _consumer_v3_date(
        portfolio_asof, label="Portfolio run asof_date"
    )
    if not effective <= portfolio_date <= expiry:
        raise ValueError("Portfolio run date falls outside Consumer registry authority dates")
    return dict(registry["cohorts"]), candidate


def validate_consumer_v3_runtime_authority(
    cfg: dict[str, Any],
    root: Path,
    *,
    source_asof: str,
    portfolio_asof: str,
) -> None:
    """Revalidate a pinned registry against both source and final Portfolio dates."""

    _load_consumer_v3_activation_registry(
        cfg,
        root,
        source_asof=source_asof,
        portfolio_asof=portfolio_asof,
    )


def validate_consumer_v3_optimizer_cap_binding(
    score_cfg: Mapping[str, Any], optimizer_cfg: Mapping[str, Any]
) -> None:
    """Bind reviewed Consumer lock caps to the independent optimizer surfaces."""

    score_caps_raw = score_cfg.get("optimizer_cap_by_scope") or {}
    optimizer_caps_root = optimizer_cfg.get("scope_weight_caps") or {}
    if not isinstance(score_caps_raw, Mapping) or not isinstance(
        optimizer_caps_root, Mapping
    ):
        raise ValueError("Consumer optimizer cap surfaces must be mappings")
    optimizer_caps_raw = optimizer_caps_root.get("consumer_defensive") or {}
    if not isinstance(optimizer_caps_raw, Mapping):
        raise ValueError("optimizer Consumer scope caps must be a mapping")
    if (
        set(score_caps_raw) != CONSUMER_DEFENSIVE_COHORTS
        or set(optimizer_caps_raw) != CONSUMER_DEFENSIVE_COHORTS
    ):
        raise ValueError("Consumer optimizer cap surfaces require the exact cohort census")
    score_caps = {
        cohort: _consumer_v3_finite(
            score_caps_raw[cohort], label=f"Consumer score cap {cohort}"
        )
        for cohort in CONSUMER_DEFENSIVE_COHORTS
    }
    optimizer_caps = {
        cohort: _consumer_v3_finite(
            optimizer_caps_raw[cohort], label=f"Consumer optimizer cap {cohort}"
        )
        for cohort in CONSUMER_DEFENSIVE_COHORTS
    }
    if any(value < 0.0 for value in [*score_caps.values(), *optimizer_caps.values()]):
        raise ValueError("Consumer optimizer caps cannot be negative")
    mismatches = [
        cohort for cohort in CONSUMER_DEFENSIVE_COHORTS
        if not math.isclose(score_caps[cohort], optimizer_caps[cohort], abs_tol=1e-12)
    ]
    if mismatches:
        raise ValueError(f"Consumer registry/optimizer scope caps diverge: {sorted(mismatches)}")
    sector_caps = optimizer_cfg.get("sector_weight_caps") or {}
    if not isinstance(sector_caps, Mapping) or "consumer_defensive" not in sector_caps:
        raise ValueError("Consumer optimizer sector cap is missing")
    sector_cap = _consumer_v3_finite(
        sector_caps["consumer_defensive"], label="Consumer optimizer sector cap"
    )
    score_sector_cap = _consumer_v3_finite(
        score_cfg.get("optimizer_sector_cap"),
        label="Consumer score-contract sector cap",
    )
    aggregate_authority = sum(score_caps.values())
    if sector_cap < 0.0 or score_sector_cap < 0.0:
        raise ValueError("Consumer sector caps cannot be negative")
    if not math.isclose(
        sector_cap, score_sector_cap, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ValueError("Consumer score/optimizer sector caps diverge")
    if aggregate_authority > 0.0 and sector_cap <= 0.0:
        raise ValueError("positive Consumer cohort authority requires a positive sector cap")
    if aggregate_authority > sector_cap + 1e-12:
        raise ValueError("Consumer cohort authority exceeds the sector cap")
    gross_exposure = _consumer_v3_finite(
        optimizer_cfg.get("gross_exposure", 1.0), label="optimizer gross exposure"
    )
    if aggregate_authority > 0.0 and not 0.0 < gross_exposure <= 1.0:
        raise ValueError(
            "positive Consumer authority requires gross exposure in (0, 1]"
        )


def _adapt_consumer_defensive(
    cfg: dict[str, Any], rows: list[dict[str, str]]
) -> list[CanonicalScore]:
    '''Adapt Consumer Defensive rows through a strict sector contract.

    A row can be investable only when upstream governance is ``promoted`` and
    the ordinary candidate/OOS gates pass. Shadow and deferred files remain
    readable for diagnostics but cannot become investable through a permissive
    Portfolio Layer configuration.
    '''

    if str(cfg.get('model_family') or '') != 'consumer_defensive':
        raise ValueError(
            'consumer_defensive adapter is reserved for consumer_defensive'
        )
    promoted_scopes = {
        str(row.get("calibration_cohort") or "").strip()
        for row in rows
        if str(row.get("promotion_state") or "").strip().lower() == "promoted"
    }
    effective_locks = dict(cfg.get("_consumer_defensive_effective_locks") or {})
    scope_contract = dict(
        cfg.get("_consumer_defensive_calibration_scope_contract") or {}
    )
    if effective_locks and set(effective_locks) != CONSUMER_DEFENSIVE_COHORTS:
        raise ValueError("Consumer Defensive effective-lock cohort census changed")
    if effective_locks and not scope_contract:
        raise ValueError(
            "Consumer Defensive v3 activation requires a verified calibration scope"
        )
    if promoted_scopes and not effective_locks:
        raise ValueError(
            "Consumer Defensive promoted rows require a cryptographically "
            "verified and configured v3 activation registry"
        )
    if rows and not promoted_scopes and not effective_locks:
        _require_zero_consumer_defensive_shadow_alpha(cfg)
    scope_calibrations = dict(cfg.get("calibration_by_scope") or {})
    scope_caps = dict(cfg.get("optimizer_cap_by_scope") or {})
    if effective_locks and (
        set(scope_calibrations) != CONSUMER_DEFENSIVE_COHORTS
        or set(scope_caps) != CONSUMER_DEFENSIVE_COHORTS
    ):
        raise ValueError("Consumer v3 activation requires exact per-cohort alpha and cap maps")
    if scope_contract:
        scope_contract = _validate_consumer_calibration_scope(scope_contract)
        row_tickers = [
            str(row.get("ticker") or "").strip().upper() for row in rows
        ]
        if any(not ticker for ticker in row_tickers) or len(row_tickers) != len(
            set(row_tickers)
        ):
            raise ValueError("Consumer Defensive score rows contain blank/duplicate tickers")
        leaked = sorted(
            set(scope_contract["excluded_tickers"]).intersection(row_tickers)
        )
        if leaked:
            raise ValueError(
                "Consumer Defensive score rows contain reviewed excluded tickers: "
                + ", ".join(leaked[:10])
            )
        observed_by_cohort = {
            cohort: sum(
                str(row.get("calibration_cohort") or "").strip() == cohort
                for row in rows
            )
            for cohort in sorted(CONSUMER_DEFENSIVE_COHORTS)
        }
        expected_by_cohort = {
            str(cohort): int(count)
            for cohort, count in scope_contract[
                "expected_remaining_current_by_cohort"
            ].items()
        }
        if (
            len(rows)
            != int(scope_contract["expected_remaining_current_ticker_count"])
            or observed_by_cohort != expected_by_cohort
            or _consumer_v3_value_sha256(sorted(row_tickers))
            != str(
                scope_contract["expected_remaining_current_tickers_sha256"]
            )
        ):
            raise ValueError(
                "Consumer Defensive score rows differ from the reviewed scope census"
            )
        if any(
            str(
                row.get("consumer_defensive_calibration_scope_sha256") or ""
            ).strip()
            != str(scope_contract["payload_sha256"])
            for row in rows
        ):
            raise ValueError(
                "Consumer Defensive score rows are not bound to the reviewed scope"
            )
    for idx, row in enumerate(rows, start=1):
        ticker = str(row.get('ticker') or '').strip().upper() or '<blank>'
        missing = sorted(set(CONSUMER_DEFENSIVE_REQUIRED_COLUMNS) - set(row))
        if missing:
            raise ValueError(
                'consumer_defensive score file row '
                f'{idx} ticker={ticker} is missing required columns: {missing}'
            )
        if str(row.get('sector') or '').strip() != 'Consumer Staples':
            raise ValueError(
                f'consumer_defensive row ticker={ticker} must map to '
                'canonical sector Consumer Staples'
            )
        state = str(row.get('promotion_state') or '').strip().lower()
        if state not in CONSUMER_DEFENSIVE_PROMOTION_STATES:
            raise ValueError(
                f'consumer_defensive row ticker={ticker} has invalid '
                f'promotion_state={state!r}'
            )
        scope = str(row.get('calibration_cohort') or '').strip()
        if scope not in CONSUMER_DEFENSIVE_COHORTS:
            raise ValueError(f'consumer_defensive row ticker={ticker} has unsupported cohort={scope!r}')
        lock = effective_locks.get(scope)
        if effective_locks:
            if not isinstance(lock, dict):
                raise ValueError(f'{ticker}: Consumer v3 row has no effective lock for cohort={scope}')
            if state != str(lock['promotion_state']):
                raise ValueError(f'{ticker}: promotion state is not bound to its Consumer v3 lock')
            for field, lock_field in CONSUMER_DEFENSIVE_V3_ROW_LOCK_FIELDS.items():
                observed = str(row.get(field) or '').strip()
                expected = str(lock[lock_field]).strip()
                if field.endswith('sha256'):
                    observed, expected = observed.lower(), expected.lower()
                if observed != expected:
                    raise ValueError(f'{ticker}: Consumer v3 row/lock mismatch for {field}')
            if str(row.get('score_model_version') or '').strip() != str(
                lock['score_model_version']
            ):
                raise ValueError(f'{ticker}: Consumer v3 row has wrong score model version')
            if str(row.get('scoring_contract_version') or '').strip() != str(
                lock['scoring_contract_version']
            ):
                raise ValueError(f'{ticker}: Consumer v3 row has wrong scoring contract version')
            row_cap = _f(row.get('consumer_defensive_optimizer_cap'))
            row_confidence = _f(row.get('consumer_defensive_confidence_multiplier'))
            if row_cap is None or not math.isclose(row_cap, float(lock['optimizer_cap']), abs_tol=1e-12):
                raise ValueError(f'{ticker}: Consumer v3 row has wrong optimizer cap')
            if row_confidence is None or not math.isclose(
                row_confidence, float(lock['confidence_multiplier']), abs_tol=1e-12
            ):
                raise ValueError(f'{ticker}: Consumer v3 row has wrong confidence multiplier')
            row_date = _consumer_v3_date(
                str(row.get('asof_date') or '').strip()[:10],
                label=f'{ticker} Consumer row asof_date',
            )
            if not _consumer_v3_date(lock['effective_from'], label='lock effective_from') <= row_date <= _consumer_v3_date(
                lock['valid_until'], label='lock valid_until'
            ):
                raise ValueError(f'{ticker}: Consumer v3 row is outside lock authority dates')
            if (state == 'promoted') is not bool(lock['investable']):
                raise ValueError(f'{ticker}: Consumer v3 investability disagrees with row state')
        elif state == 'promoted':
            raise ValueError(f'{ticker}: promoted Consumer row has no verified v3 registry')

        expected_alpha = float(lock['expected_alpha_at_full']) if state == 'promoted' and lock else 0.0
        expected_cap = float(lock['optimizer_cap']) if state == 'promoted' and lock else 0.0
        if scope_calibrations:
            scope_calib = dict(scope_calibrations.get(scope) or {})
            configured_alpha = _f(scope_calib.get('expected_alpha_at_full'))
            if configured_alpha is None or not math.isclose(configured_alpha, expected_alpha, abs_tol=1e-12):
                raise ValueError(f'{scope}: configured alpha is not bound to Consumer v3 authority')
        if scope_caps:
            configured_cap = _f(scope_caps.get(scope))
            if configured_cap is None or not math.isclose(configured_cap, expected_cap, abs_tol=1e-12):
                raise ValueError(f'{scope}: configured cap is not bound to Consumer v3 authority')
        if _truthy(row.get('portfolio_candidate_gate')) and state != 'promoted':
            raise ValueError(
                f'consumer_defensive row ticker={ticker} asserts a candidate '
                f'gate while promotion_state={state!r}'
            )
        if not effective_locks and _truthy(row.get('oos_score_valid_flag')) and state != 'promoted':
            raise ValueError(
                f'consumer_defensive row ticker={ticker} asserts OOS validity '
                f'while promotion_state={state!r}'
            )
        if not effective_locks and state != 'promoted' and (
            _truthy(row.get('research_calibration_input_eligible_flag'))
            or _truthy(row.get('stage11_calibration_input_eligible_flag'))
            or _truthy(row.get('survivorship_corrected_panel_flag'))
        ):
            raise ValueError(
                f'consumer_defensive row ticker={ticker} asserts research '
                f'lineage while promotion_state={state!r}'
            )
    return _adapt_final_rank_family(
        cfg, rows, enforce_candidate_status=True
    )


def _adapt_biotech(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    score_fields = {
        str(row.get("production_rank_score_field", "")).strip()
        for row in rows
        if str(row.get("production_rank_score_field", "")).strip()
    }
    if len(score_fields) > 1:
        raise ValueError(f"biotech production_rank_score_field must be unanimous; found={sorted(score_fields)}")
    score_field = next(iter(score_fields), "opportunity_score")
    zero_is_missing = bool(cfg.get("zero_score_is_missing", score_field in {"opportunity_score", "production_rank_score"}))
    has_standard_contract = bool(rows and "portfolio_candidate_gate" in rows[0])
    out: list[CanonicalScore] = []
    skipped = 0
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        if has_standard_contract:
            native_score_field = str(r.get("native_score_field") or score_field or "opportunity_score").strip()
            native_candidates = [
                r.get("native_score_value"),
                r.get(native_score_field) if native_score_field else None,
                r.get("opportunity_score"),
                r.get("production_rank_score"),
                r.get(score_field) if score_field != native_score_field else None,
                r.get("portfolio_candidate_score"),
            ]
        else:
            native_candidates = [
                r.get(score_field),
                r.get("production_rank_score"),
            ]
        native = next((value for value in (_f(candidate) for candidate in native_candidates) if value is not None), None)
        if not ticker or native is None:
            skipped += 1
            continue
        score_missing_raw = str(r.get("score_zero_is_missing_flag") or "").strip()
        if has_standard_contract and score_missing_raw:
            # Standardized sector files explicitly adjudicate whether a numeric zero is
            # computed or missing. Do not re-interpret an explicit 0 as a sentinel.
            missing_score = _truthy(score_missing_raw)
        else:
            missing_score = bool(zero_is_missing and native == 0.0)
        calibration_ok = _truthy(r.get("calibration_eligible_flag")) if has_standard_contract else True
        price_data_available = bool(
            str(r.get("price_data_asof_date") or r.get("latest_price_date") or "").strip()
        )
        if has_standard_contract:
            status = str(r.get("portfolio_candidate_status") or "").strip().lower()
            status_allows = status in {"", "eligible"}
            eligible = _truthy(r.get("portfolio_candidate_gate")) and status_allows and not missing_score
            if eligible:
                # the sector gate already adjudicated this name as a passing candidate; a soft source
                # annotation (e.g. "ok:no_guidance_negative_growth_cap" where the quality cap did NOT
                # veto) must not leak into the contract's authoritative eligibility_reason, which the
                # Stage 1 invariant requires to be exactly "ok" for investable names. Mirrors the
                # tech_family adapter, which also forces "ok" for eligible rows.
                reason = "ok"
            else:
                reason = str(
                    r.get("eligibility_reason")
                    or r.get("portfolio_candidate_reason")
                    or "failed_portfolio_candidate_gate"
                ).strip()
            if missing_score:
                eligible = False
                reason = "missing_score"
        else:
            if missing_score:
                eligible = False
                reason = "missing_score"
            else:
                investible = _truthy(r.get("biotech_cohort_investible_flag"))
                vetoed = _truthy(r.get("core_structural_veto_flag"))
                eligible = investible and not vetoed
                reason = "ok" if eligible else ("core_structural_veto" if vetoed else "not_investible")
        raw_research_eligible = bool(calibration_ok and not missing_score and price_data_available)
        oos_score_valid = _oos_score_valid(r, cfg, default_requires_oos=True)
        if eligible and not oos_score_valid:
            # investable_eligible must imply oos_score_valid_flag=1 (docs/score_eligibility_flags.md)
            eligible = False
            reason = f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}"
        raw_source_research_eligible = _truthy(
            r.get("stage11_calibration_input_eligible_flag")
            if "stage11_calibration_input_eligible_flag" in r
            else r.get("research_calibration_input_eligible_flag")
        )
        source_role = _sample_role(r, investable=eligible, research_eligible=raw_source_research_eligible)
        if "stage11_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("stage11_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and not missing_score
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(
                r.get("stage11_calibration_input_reason")
                or r.get("research_calibration_reason")
                or ""
            ).strip()
            if not research_reason:
                research_reason = "ok" if research_eligible else "excluded_by_calibration_policy"
            if raw_research_eligible and not research_eligible:
                research_reason = "missing_score" if missing_score else _research_guard_reason(
                    r,
                    source_role=source_role,
                    oos_score_valid=oos_score_valid,
                )
        elif "research_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("research_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and not missing_score
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(r.get("research_calibration_reason") or "").strip()
            if not research_reason:
                research_reason = "ok" if research_eligible else "not_calibration_research_eligible"
            if raw_research_eligible and not research_eligible:
                research_reason = "missing_score" if missing_score else _research_guard_reason(
                    r,
                    source_role=source_role,
                    oos_score_valid=oos_score_valid,
                )
        else:
            research_eligible = raw_research_eligible
            if research_eligible:
                research_reason = "ok"
            elif not calibration_ok:
                research_reason = "not_calibration_eligible"
            elif missing_score:
                research_reason = "missing_score"
            elif not price_data_available:
                research_reason = "missing_price_data"
            else:
                research_reason = "not_calibration_research_eligible"
        if eligible:
            research_eligible = True
            research_reason = "ok"
        industry = str(r.get("biotech_primary_cohort", "")).strip() or str(cfg["industry"])
        conf = _confidence(
            r.get("score_confidence")
            if has_standard_contract and str(r.get("score_confidence", "")).strip()
            else r.get("data_quality_confidence_multiplier")
        )
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=industry,
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            calibration_research_eligible=int(research_eligible),
            calibration_research_reason=research_reason,
            calibration_sample_role=source_role,
            stage1_sample_role=_contract_sample_role(
                source_role,
                investable=eligible,
                research_eligible=research_eligible,
                oos_score_valid=oos_score_valid,
            ),
            oos_score_valid_flag=int(oos_score_valid),
            missing_score_flag=int(missing_score),
            survivorship_corrected_panel_flag=int(_survivorship_corrected(r)),
            score_confidence=conf,
            source_asof_date=_source_asof(r),
            model_scope_id=industry,
            production_policy_id=str(
                r.get("biotech_promotion_contract_id") or r.get("tier1_selection_policy") or ""
            ).strip(),
            production_policy_sha256=str(r.get("biotech_promotion_contract_sha256") or "").strip().lower(),
            selection_reliability_class=str(
                r.get("biotech_selection_reliability_class") or ""
            ).strip().lower(),
            active_sleeve_weight=_f(r.get("biotech_active_sleeve_weight")),
            active_name_weight_cap=_f(r.get("biotech_max_name_weight_within_cohort")),
            active_selected_name_count=_f(r.get("biotech_selected_name_count_within_cohort")),
            benchmark_residual_weight=_f(r.get("biotech_xbi_residual_weight")),
            benchmark_residual_ticker=(
                str(cfg.get("benchmark_residual_ticker") or "XBI").strip().upper()
                if str(r.get("biotech_xbi_residual_weight") or "").strip()
                else ""
            ),
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "biotech"), skipped)
    return out


def _med_device_score_provenance_unsafe(row: dict[str, str]) -> bool:
    mode = str(row.get("ic_tilted_composite_mode") or "").strip().lower()
    source = str(row.get("production_score_source") or "").strip().lower()
    applied = _truthy(row.get("ic_tilt_applied_to_production_flag"))
    return bool(
        mode == "replace_raw"
        or applied
        or source in {"ic_tilted_composite", "ic_tilted_composite_score"}
    )


def _adapt_med_devices(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    if rows and "portfolio_candidate_gate" not in rows[0]:
        raise ValueError("med_devices score file must include portfolio_candidate_gate")
    explicit_confidence_field = next(
        (
            field
            for field in ("score_confidence", "portfolio_score_confidence", "model_confidence")
            if rows and field in rows[0]
        ),
        "",
    )
    completeness_values = [
        value
        for value in (_f(r.get("data_completeness_score")) for r in rows)
        if value is not None
    ]
    constant_completeness = (
        bool(completeness_values)
        and len({round(value, 8) for value in completeness_values}) == 1
    )
    lineage_policy = policy_for_model_family(str(cfg.get("model_family") or "med_devices"))
    lineage_policy_mode = str(
        cfg.get("_financial_lineage_policy_mode") or POLICY_DISABLED
    )
    out: list[CanonicalScore] = []
    skipped = 0
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = next(
            (
                value
                for value in (
                    _f(r.get("native_score_value")),
                    _f(r.get("portfolio_candidate_score")),
                    _f(r.get("composite_score")),
                )
                if value is not None
            ),
            None,
        )
        if not ticker or native is None:
            skipped += 1
            continue
        missing_score = native <= 0.0
        unsafe_score_provenance = _med_device_score_provenance_unsafe(r)
        source_candidate = _truthy(r.get("portfolio_candidate_gate"))
        lineage_required = lineage_policy_mode == POLICY_STRICT_UNIVERSE or (
            lineage_policy_mode == POLICY_CANDIDATE_ONLY and source_candidate
        )
        lineage_ok = not lineage_required or lineage_row_is_safe(
            r,
            expected_asof=str(r.get("asof_date") or "").strip()[:10],
            min_core_metric_count=lineage_policy.min_core_metric_count,
        )
        eligible = (
            source_candidate
            and not missing_score
            and not unsafe_score_provenance
            and lineage_ok
        )
        reason = (
            "ok"
            if eligible
            else "unsafe_production_score_provenance"
            if unsafe_score_provenance
            else "missing_score"
            if missing_score
            else f"financial_lineage:{str(r.get('financial_lineage_status') or 'missing')}"
            if not lineage_ok
            else str(r.get("portfolio_candidate_reason") or "failed_portfolio_candidate_gate").strip()
        )
        calibration_ok = _truthy(r.get("calibration_eligible_flag")) if "calibration_eligible_flag" in r else eligible
        oos_score_valid = bool(
            _oos_score_valid(r, cfg, default_requires_oos=True) and not unsafe_score_provenance
        )
        if eligible and not oos_score_valid:
            # investable_eligible must imply oos_score_valid_flag=1 (docs/score_eligibility_flags.md);
            # pre-lock historical exports may set the gate while the score is not yet a frozen OOS model.
            eligible = False
            reason = f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}"
        raw_source_research_eligible = _truthy(
            r.get("stage11_calibration_input_eligible_flag")
            if "stage11_calibration_input_eligible_flag" in r
            else r.get("research_calibration_input_eligible_flag")
        )
        source_role = _sample_role(r, investable=eligible, research_eligible=raw_source_research_eligible)
        if unsafe_score_provenance:
            source_role = "excluded"
        if "stage11_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("stage11_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and not missing_score
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(
                r.get("stage11_calibration_input_reason")
                or r.get("research_calibration_reason")
                or r.get("research_calibration_status")
                or ""
            ).strip()
            if research_eligible and research_reason == "valid_research_calibration_input":
                research_reason = "ok"
            elif not research_eligible:
                research_reason = (
                    "missing_score"
                    if missing_score
                    else _research_guard_reason(r, source_role=source_role, oos_score_valid=oos_score_valid)
                    if raw_research_eligible
                    else research_reason
                    or "not_calibration_research_eligible"
                )
        elif "research_calibration_input_eligible_flag" in r:
            raw_research_eligible = _truthy(r.get("research_calibration_input_eligible_flag"))
            research_eligible = bool(
                raw_research_eligible
                and not missing_score
                and _stage11_research_allowed(r, source_role=source_role, oos_score_valid=oos_score_valid)
            )
            research_reason = str(
                r.get("research_calibration_reason")
                or r.get("research_calibration_status")
                or ""
            ).strip()
            if research_eligible and research_reason == "valid_research_calibration_input":
                research_reason = "ok"
            elif not research_eligible:
                research_reason = (
                    "missing_score"
                    if missing_score
                    else _research_guard_reason(r, source_role=source_role, oos_score_valid=oos_score_valid)
                    if raw_research_eligible
                    else research_reason
                    or "not_calibration_research_eligible"
                )
        else:
            research_eligible = bool(calibration_ok and not missing_score)
            research_reason = _research_reason(
                eligible=research_eligible,
                reason="not_calibration_eligible" if not calibration_ok else "missing_score",
            )
        if unsafe_score_provenance:
            research_eligible = False
            research_reason = "unsafe_production_score_provenance"
        if eligible:
            research_eligible = True
            research_reason = "ok"
        industry = str(r.get("subsector", "")).strip() or str(cfg["industry"])
        if explicit_confidence_field:
            conf = _confidence(r.get(explicit_confidence_field))
        elif constant_completeness:
            # A constant 100% completeness field is not a cross-sector-comparable confidence signal.
            conf = 0.5
        else:
            conf = _confidence(r.get("data_completeness_score"), denominator=100.0)
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=industry,
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            calibration_research_eligible=int(research_eligible),
            calibration_research_reason=research_reason,
            calibration_sample_role=source_role,
            stage1_sample_role=_contract_sample_role(
                source_role,
                investable=eligible,
                research_eligible=research_eligible,
                oos_score_valid=oos_score_valid,
            ),
            oos_score_valid_flag=int(oos_score_valid),
            missing_score_flag=int(missing_score),
            survivorship_corrected_panel_flag=int(_survivorship_corrected(r)),
            score_confidence=conf,
            source_asof_date=_source_asof(r),
            financial_lineage_checked_asof_date=str(
                r.get("financial_lineage_checked_asof_date") or ""
            ),
            financial_lineage_status=str(r.get("financial_lineage_status") or ""),
            financial_lineage_gate=int(
                _truthy(r.get("financial_lineage_gate"))
            ),
            financial_lineage_classification=str(
                r.get("financial_lineage_classification") or ""
            ),
            latest_material_financial_filing_date=str(
                r.get("latest_material_financial_filing_date") or ""
            ),
            latest_material_financial_form=str(
                r.get("latest_material_financial_form") or ""
            ),
            latest_material_financial_accession=str(
                r.get("latest_material_financial_accession") or ""
            ),
            latest_material_financial_report_date=str(
                r.get("latest_material_financial_report_date") or ""
            ),
            incorporated_financial_filing_date=str(
                r.get("incorporated_financial_filing_date") or ""
            ),
            incorporated_financial_accession=str(
                r.get("incorporated_financial_accession") or ""
            ),
            incorporated_financial_report_date=str(
                r.get("incorporated_financial_report_date") or ""
            ),
            incorporated_financial_core_metric_count=int(
                _f(r.get("incorporated_financial_core_metric_count")) or 0
            ),
            financial_lineage_reason=str(r.get("financial_lineage_reason") or ""),
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "med_devices"), skipped)
    return out


_ADAPTERS: dict[str, Callable[[dict[str, Any], list[dict[str, str]]], list[CanonicalScore]]] = {
    "tech_family": _adapt_tech_family,
    "industrial_family": _adapt_industrial_family,
    "transportation_subgroup": _adapt_transportation_subgroup,
    "consumer_defensive": _adapt_consumer_defensive,
    "biotech": _adapt_biotech,
    "med_devices": _adapt_med_devices,
}

RESERVED_MODEL_ADAPTERS = {
    "consumer_defensive": "consumer_defensive",
    "transportation": "transportation_subgroup",
}


RANK_TABLE_SUFFIX = "_final_rank_table.csv"
STAGE11_SIDECAR_SUFFIX = "_stage11_survivorship_calibration_panel.csv"
_CHUNK_RANGE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$")


def _snapshot_iso_date(source_file: Path) -> str:
    """ISO date of the dated folder a resolved sector file lives in ('' for flat files)."""
    name = source_file.parent.name
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
        return name
    if re.fullmatch(r"\d{8}", name):
        return f"{name[:4]}-{name[4:6]}-{name[6:]}"
    return ""


def _resolve_financial_lineage_sidecar(
    cfg: dict[str, Any],
    root: Path,
    *,
    source_asof: str,
) -> Path:
    template = str(cfg.get("financial_lineage_file_path") or "").strip()
    if not template:
        raise ValueError(
            f"{cfg.get('model_family')} production lineage requires "
            "financial_lineage_file_path"
        )
    if "{yyyy-mm-dd}" in template:
        relative = template.replace("{yyyy-mm-dd}", source_asof)
    elif "{yyyymmdd}" in template:
        relative = template.replace("{yyyymmdd}", source_asof.replace("-", ""))
    else:
        raise ValueError(
            "financial_lineage_file_path must contain {yyyy-mm-dd} or {yyyymmdd}: "
            f"{template}"
        )
    root_resolved = root.resolve()
    path = (root_resolved / relative).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"financial lineage sidecar escapes sector output root: {path}"
        ) from exc
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing production financial lineage sidecar for "
            f"{cfg.get('model_family')} asof={source_asof}: {path}"
        )
    return path


def _merge_financial_lineage_sidecar(
    rank_rows: list[dict[str, str]],
    lineage_rows: list[dict[str, str]],
    *,
    model_family: str,
    source_asof: str,
) -> list[dict[str, str]]:
    alignment_errors = financial_lineage_sidecar_alignment_errors(
        rank_rows,
        lineage_rows,
        expected_asof=source_asof,
    )
    if alignment_errors:
        raise ValueError(
            f"{model_family} lineage/rank mismatch: {alignment_errors[:20]}"
        )
    lineage_by_ticker = {
        str(row["ticker"]).strip().upper(): row for row in lineage_rows
    }

    merged: list[dict[str, str]] = []
    for rank_row in rank_rows:
        ticker = str(rank_row["ticker"]).strip().upper()
        lineage_row = lineage_by_ticker[ticker]
        output = dict(rank_row)
        output.update(
            {
                field: str(lineage_row.get(field) or "")
                for field in FINANCIAL_LINEAGE_FIELDS
            }
        )
        merged.append(output)
    return merged


def stage11_sidecar_only_rows(source_file: Path, as_of_iso: str) -> tuple[list[dict[str, str]], Path | None]:
    """Survivorship-corrected Stage 11 panel rows for exactly `as_of_iso` from the best source.

    Source precedence: legacy per-date sidecar next to the rank table, else the stage11_combined
    range chunk containing the date (latest range end wins), else the dashboard-root consolidated
    panel. Returns ([], None) when no source covers the date â€” tech rows then simply have no
    survivorship-corrected calibration path for that day (flagged downstream, never fabricated).
    """
    if RANK_TABLE_SUFFIX not in source_file.name or not as_of_iso:
        return [], None
    prefix = source_file.name.replace(RANK_TABLE_SUFFIX, "")
    candidates: list[Path] = []
    legacy = source_file.with_name(f"{prefix}{STAGE11_SIDECAR_SUFFIX}")
    if legacy.exists():
        candidates.append(legacy)
    dashboard_root = source_file.parent.parent
    chunk_dir = dashboard_root / "stage11_combined"
    if chunk_dir.exists():
        in_range = []
        for path in chunk_dir.glob(f"{prefix}_stage11_survivorship_calibration_panel_*.csv"):
            m = _CHUNK_RANGE_RE.search(path.name)
            if m and m.group(1) <= as_of_iso <= m.group(2):
                in_range.append((m.group(2), path))
        # widest/latest covering chunk first: a regenerated monthly chunk supersedes an older,
        # narrower test chunk covering the same date
        candidates.extend(path for _end, path in sorted(in_range, reverse=True))
    root_panel = dashboard_root / f"{prefix}{STAGE11_SIDECAR_SUFFIX}"
    if root_panel.exists():
        candidates.append(root_panel)
    for path in candidates:
        rows = [r for r in read_csv(path) if str(r.get("asof_date", "")).strip()[:10] == as_of_iso]
        if rows:
            return rows, path
    return [], None


def _as_stage11_calibration_only(row: dict[str, str]) -> dict[str, str]:
    """Demote a sidecar-only row so research evidence cannot enter production.

    A ticker absent from the dated final-rank table has no production score or
    lineage authority for that date. Stage 11 may still consume its certified
    survivorship row, but the portfolio candidate contract must fail closed.
    """
    output = dict(row)
    output.update(
        {
            "portfolio_candidate_gate": "0",
            "rank_ready_flag": "0",
            "portfolio_candidate_status": "excluded",
            "portfolio_candidate_reason": "stage11_sidecar_calibration_only",
            "investable_eligible": "0",
            "investable_eligible_flag": "0",
            "production_eligible_flag": "0",
        }
    )
    return output


def run_adapter(cfg: dict[str, Any], sector_output_root: Path, run_as_of: str | None) -> AdapterResult:
    adapter_name = str(cfg["adapter"])
    if adapter_name not in _ADAPTERS:
        raise ValueError(f"Unknown adapter '{adapter_name}' for {cfg.get('model_family')}")
    model_family = str(cfg.get("model_family") or "").strip()
    required_adapter = RESERVED_MODEL_ADAPTERS.get(model_family)
    if required_adapter is not None and adapter_name != required_adapter:
        raise ValueError(
            f"{model_family} is reserved for adapter={required_adapter}; "
            f"configured adapter={adapter_name} would bypass promotion governance"
        )
    source_file = _resolve_file(cfg, sector_output_root, run_as_of)
    consumer_fieldnames: tuple[str, ...] | None = None
    consumer_source_sha256 = ""
    if adapter_name == "consumer_defensive":
        rows, consumer_fieldnames, consumer_source_sha256 = (
            _consumer_v3_read_csv_snapshot(source_file)
        )
    else:
        rows = read_csv(source_file)
    effective_cfg = dict(cfg)
    source_asof = _snapshot_iso_date(source_file) or _dominant(rows, "asof_date")
    consumed_consumer_registry: Path | None = None
    consumed_consumer_score_manifest: Path | None = None
    consumed_consumer_terminal_manifest: Path | None = None
    if adapter_name == "consumer_defensive":
        production_authority_requested = any(
            str(cfg.get(field) or "").strip()
            for field in (
                "production_score_manifest_filename",
                "production_calibration_scope_sha256",
                "production_terminal_manifest_file_path",
                "production_activation_registry_file_path",
                "production_activation_registry_sha256",
            )
        ) or any(
            str(row.get("promotion_state") or "").strip().lower()
            == "promoted"
            for row in rows
        )
        if production_authority_requested:
            if consumer_fieldnames != CONSUMER_DEFENSIVE_V3_PRODUCTION_COLUMNS:
                raise ValueError(
                    "Consumer production score CSV schema differs from v3 contract"
                )
            (
                scope_contract,
                consumed_consumer_score_manifest,
                consumer_score_manifest,
                consumer_score_manifest_file_sha256,
            ) = _load_consumer_v3_score_manifest(
                cfg,
                source_file,
                rows,
                source_asof=source_asof,
                source_file_sha256=consumer_source_sha256,
            )
            consumed_consumer_terminal_manifest = (
                _load_consumer_v3_terminal_manifest(
                    cfg,
                    sector_output_root,
                    source_asof=source_asof,
                    source_file=source_file,
                    source_file_sha256=consumer_source_sha256,
                    source_row_count=len(rows),
                    score_manifest_path=consumed_consumer_score_manifest,
                    score_manifest=consumer_score_manifest,
                    score_manifest_file_sha256=(
                        consumer_score_manifest_file_sha256
                    ),
                    calibration_scope_sha256=str(
                        scope_contract["payload_sha256"]
                    ),
                )
            )
            effective_cfg[
                "_consumer_defensive_calibration_scope_contract"
            ] = scope_contract
        effective_locks, consumed_consumer_registry = (
            _load_consumer_v3_activation_registry(
                cfg,
                sector_output_root,
                source_asof=source_asof,
                portfolio_asof=run_as_of or source_asof,
            )
        )
        effective_cfg["_consumer_defensive_effective_locks"] = effective_locks
    lineage_policy = policy_for_model_family(str(cfg.get("model_family") or ""))
    lineage_policy_mode = lineage_policy.mode_for_asof("production", source_asof)
    effective_cfg["_financial_lineage_policy_mode"] = lineage_policy_mode
    consumed_lineage_sidecar: Path | None = None
    if (
        lineage_policy_mode != POLICY_DISABLED
        and str(cfg.get("financial_lineage_file_path") or "").strip()
    ):
        consumed_lineage_sidecar = _resolve_financial_lineage_sidecar(
            cfg,
            sector_output_root,
            source_asof=source_asof,
        )
        rows = _merge_financial_lineage_sidecar(
            rows,
            read_csv(consumed_lineage_sidecar),
            model_family=str(cfg.get("model_family") or ""),
            source_asof=source_asof,
        )
    consumed_stage11_sidecar: Path | None = None
    if (
        adapter_name in {
            "tech_family", "industrial_family", "transportation_subgroup",
            "consumer_defensive",
        }
        and not (adapter_name == "consumer_defensive" and consumed_consumer_registry)
    ):
        # Final-rank tables may replay the CURRENT universe, so delisted members can exist only in
        # Stage 11 survivorship panels. Merge sidecar-only rows the sector itself certifies as
        # calibration inputs (stage11_calibration_input_eligible_flag=1) so they flow through the
        # contract as calibration-only rows: their gate/oos flags keep them non-investable, and
        # the survivorship-corrected stamp carries their research eligibility.
        as_of_iso = _snapshot_iso_date(source_file)
        sidecar_rows, sidecar_source = stage11_sidecar_only_rows(source_file, as_of_iso)
        if sidecar_rows:
            present = {str(r.get("ticker", "")).strip().upper() for r in rows}
            merged: dict[str, dict[str, str]] = {}
            skipped_ineligible = 0
            for r in sidecar_rows:
                ticker = str(r.get("ticker", "")).strip().upper()
                if not ticker or ticker in present:
                    continue
                if not _truthy(r.get("stage11_calibration_input_eligible_flag")):
                    skipped_ineligible += 1
                    continue
                merged[ticker] = _as_stage11_calibration_only(r)
            if merged:
                LOGGER.info(
                    "Adapter %s merged %d sidecar-only calibration rows for %s "
                    "(skipped_ineligible=%d, source=%s)",
                    cfg.get("model_family"), len(merged), as_of_iso, skipped_ineligible,
                    sidecar_source.name if sidecar_source else "",
                )
                rows = rows + list(merged.values())
                consumed_stage11_sidecar = sidecar_source
    canonical = _ADAPTERS[adapter_name](effective_cfg, rows)
    canonical_asof = (
        _dominant([{"d": c.source_asof_date} for c in canonical], "d")
        if canonical
        else ""
    )
    source_files = tuple(
        dict.fromkeys(
            path
            for path in (
                source_file,
                consumed_consumer_score_manifest,
                consumed_consumer_terminal_manifest,
                consumed_consumer_registry,
                consumed_lineage_sidecar,
                consumed_stage11_sidecar,
            )
            if path is not None
        )
    )
    return AdapterResult(
        source_pipeline=str(cfg["model_family"]),
        adapter=adapter_name,
        source_file=source_file,
        source_asof_date=canonical_asof,
        rows=canonical,
        source_files=source_files,
    )
