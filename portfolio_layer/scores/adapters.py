"""Per-sector adapters: read a sector's published score CSV and map it onto CanonicalScore rows.

Three shapes cover all sub-sectors:
  - tech_family : semiconductors, software_infrastructure, technology_hardware (shared `final_score`)
  - biotech     : biotech_index daily scores using `portfolio_candidate_*` when present
  - med_devices : med_device daily composite scores gated by `portfolio_candidate_gate`

Adapters read files only. They never import a sector package or open a sector DB.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Callable

from portfolio_layer.core.contracts import AdapterResult, CanonicalScore
from portfolio_layer.core.contracts import read_csv


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


def _resolve_file(cfg: dict[str, Any], root: Path, run_as_of: str | None) -> Path:
    """Resolve a flat file or the dated-folder file for the run as-of (or latest <= as-of)."""
    mode = str(cfg.get("file_mode", "flat"))
    rel = str(cfg["file_path"])
    if mode == "flat":
        return (root / rel).resolve()
    if mode == "dated":
        # The date token occupies one whole path segment (the dated folder name).
        # Existing biotech outputs use YYYYMMDD; newer technology snapshots use YYYY-MM-DD.
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
        if not candidates:
            raise FileNotFoundError(f"No dated score file found for pattern {rel} under {parent_dir}")
        target = run_as_of.replace("-", "") if run_as_of else None
        eligible = [c for c in candidates if target is None or c[0] <= target]
        if target is not None and not eligible:
            earliest = min(c[0] for c in candidates)
            raise FileNotFoundError(
                f"No dated score file at or before {target} for pattern {rel} under {parent_dir}; "
                f"earliest available is {earliest}"
            )
        chosen = max(eligible, key=lambda c: c[0])
        return chosen[1]
    raise ValueError(f"Unknown file_mode: {mode}")


def _adapt_tech_family(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    out: list[CanonicalScore] = []
    require_oos_score_valid = bool(cfg.get("require_oos_score_valid", False))
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("final_score"))
        if not ticker or native is None:
            continue
        rank_ready = _truthy(r.get("rank_ready_flag"))
        calib_ok = _truthy(r.get("calibration_eligible_flag"))
        complete = str(r.get("model_status", "")).strip().lower() == "complete"
        oos_score_valid = _truthy(r.get("oos_score_valid_flag")) if "oos_score_valid_flag" in r else not require_oos_score_valid
        eligible = rank_ready and calib_ok and complete and (oos_score_valid or not require_oos_score_valid)
        reason = "ok" if eligible else (
            "not_rank_ready" if not rank_ready else
            "not_calibration_eligible" if not calib_ok else
            "model_incomplete" if not complete else
            f"not_oos_score_valid:{str(r.get('oos_invalid_reason') or '').strip()[:180]}"
        )
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=str(cfg["industry"]),
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            score_confidence=_confidence(r.get("data_quality_confidence")),
            source_asof_date=_source_asof(r),
        ))
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
            continue
        score_zero_is_missing = _truthy(r.get("score_zero_is_missing_flag"))
        missing_score = score_zero_is_missing or bool(zero_is_missing and native == 0.0)
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
            score_confidence=conf,
            source_asof_date=_source_asof(r),
        ))
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
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("portfolio_candidate_score") or r.get("composite_score"))
        if not ticker or native is None:
            continue
        eligible = _truthy(r.get("portfolio_candidate_gate"))
        reason = (
            "ok"
            if eligible
            else str(r.get("portfolio_candidate_reason") or "failed_portfolio_candidate_gate").strip()
        )
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
            score_confidence=conf, source_asof_date=_source_asof(r),
        ))
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
