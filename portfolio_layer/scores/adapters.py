"""Per-sector adapters: read a sector's published score CSV and map it onto CanonicalScore rows.

Three shapes cover all sub-sectors:
  - tech_family : semiconductors, software_infrastructure, technology_hardware (shared `final_score`)
  - biotech     : biotech_index daily scores using `portfolio_candidate_*` when present
  - med_devices : med_device daily composite scores gated by `portfolio_candidate_gate`

Adapters read files only. They never import a sector package or open a sector DB.
"""
from __future__ import annotations

import math
import logging
import re
from pathlib import Path
from typing import Any, Callable

from portfolio_layer.core.contracts import AdapterResult, CanonicalScore
from portfolio_layer.core.contracts import read_csv


LOGGER = logging.getLogger(__name__)


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


def _has_column_value(row: dict[str, str], column: str) -> bool:
    return column in row and str(row.get(column, "")).strip() != ""


def _oos_score_valid(row: dict[str, str], cfg: dict[str, Any], *, default_requires_oos: bool = True) -> bool:
    require_oos = bool(cfg.get("require_oos_score_valid", default_requires_oos))
    if _has_column_value(row, "oos_score_valid_flag"):
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
    return bool((source_role == "strict_oos" and oos_score_valid) or _survivorship_corrected(row))


def _research_guard_reason(row: dict[str, str], *, source_role: str, oos_score_valid: bool) -> str:
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


def _adapt_tech_family(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    out: list[CanonicalScore] = []
    require_oos_score_valid = bool(cfg.get("require_oos_score_valid", False))
    skipped = 0
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("final_score"))
        if not ticker or native is None:
            skipped += 1
            continue
        rank_ready = _truthy(r.get("rank_ready_flag"))
        calib_ok = _truthy(r.get("calibration_eligible_flag"))
        complete = str(r.get("model_status", "")).strip().lower() == "complete"
        oos_score_valid = _oos_score_valid(r, cfg, default_requires_oos=require_oos_score_valid)
        if "portfolio_candidate_gate" in r:
            eligible = _truthy(r.get("portfolio_candidate_gate"))
        else:
            eligible = rank_ready and calib_ok and complete and (oos_score_valid or not require_oos_score_valid)
        demoted_not_oos = bool(eligible and not oos_score_valid)
        if eligible and not oos_score_valid:
            eligible = False
        source_reason = str(r.get("portfolio_candidate_reason") or "").strip()
        reason = (
            "ok" if eligible else
            f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}" if demoted_not_oos else
            source_reason if source_reason and source_reason.lower() != "ok" else
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
            if eligible:
                research_eligible = True
            research_reason = str(r.get("research_calibration_reason") or r.get("calibration_status_reason") or "").strip()
            if not research_reason:
                research_reason = _research_reason(
                    eligible=research_eligible,
                    reason=str(r.get("research_calibration_status") or "not_calibration_research_eligible"),
                )
            if raw_research_eligible and not research_eligible:
                research_reason = _research_guard_reason(r, source_role=source_role, oos_score_valid=oos_score_valid)
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
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "tech_family"), skipped)
    return out


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
        score_zero_is_missing = _truthy(r.get("score_zero_is_missing_flag"))
        missing_score = score_zero_is_missing or bool(zero_is_missing and native == 0.0)
        calibration_ok = _truthy(r.get("calibration_eligible_flag")) if has_standard_contract else True
        price_data_available = bool(
            str(r.get("price_data_asof_date") or r.get("latest_price_date") or "").strip()
        )
        if has_standard_contract:
            status = str(r.get("portfolio_candidate_status") or "").strip().lower()
            status_allows = status in {"", "eligible"}
            eligible = _truthy(r.get("portfolio_candidate_gate")) and status_allows and not missing_score
            reason = str(
                r.get("eligibility_reason")
                or r.get("portfolio_candidate_reason")
                or ("ok" if eligible else "failed_portfolio_candidate_gate")
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
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "biotech"), skipped)
    return out


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
        eligible = _truthy(r.get("portfolio_candidate_gate")) and not missing_score
        reason = (
            "ok"
            if eligible
            else "missing_score" if missing_score else str(r.get("portfolio_candidate_reason") or "failed_portfolio_candidate_gate").strip()
        )
        calibration_ok = _truthy(r.get("calibration_eligible_flag")) if "calibration_eligible_flag" in r else eligible
        oos_score_valid = _oos_score_valid(r, cfg, default_requires_oos=True)
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
            score_confidence=conf, source_asof_date=_source_asof(r),
        ))
    if skipped:
        LOGGER.warning("Adapter %s skipped %d rows with blank ticker or non-finite native score",
                       cfg.get("model_family", "med_devices"), skipped)
    return out


_ADAPTERS: dict[str, Callable[[dict[str, Any], list[dict[str, str]]], list[CanonicalScore]]] = {
    "tech_family": _adapt_tech_family,
    "biotech": _adapt_biotech,
    "med_devices": _adapt_med_devices,
}


def run_adapter(cfg: dict[str, Any], sector_output_root: Path, run_as_of: str | None) -> AdapterResult:
    adapter_name = str(cfg["adapter"])
    if adapter_name not in _ADAPTERS:
        raise ValueError(f"Unknown adapter '{adapter_name}' for {cfg.get('model_family')}")
    source_file = _resolve_file(cfg, sector_output_root, run_as_of)
    rows = read_csv(source_file)
    canonical = _ADAPTERS[adapter_name](cfg, rows)
    source_asof = _dominant([{"d": c.source_asof_date} for c in canonical], "d") if canonical else ""
    return AdapterResult(
        source_pipeline=str(cfg["model_family"]),
        adapter=adapter_name,
        source_file=source_file,
        source_asof_date=source_asof,
        rows=canonical,
    )
