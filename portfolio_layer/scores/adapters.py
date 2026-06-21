"""Per-sector adapters: read a sector's published score CSV and map it onto CanonicalScore rows.

Three shapes cover all sub-sectors:
  - tech_family : semiconductors, software_infrastructure, technology_hardware (shared `final_score`)
  - biotech     : biotech_index daily scores (headline named by `production_rank_score_field`)
  - med_devices : med_device daily composite scores

Adapters read files only. They never import a sector package or open a sector DB.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from portfolio_layer.core.contracts import AdapterResult, CanonicalScore
from portfolio_layer.core.contracts import read_csv


def _f(value: Any) -> float | None:
    try:
        return None if value is None or str(value).strip() == "" else float(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in ("1", "1.0", "true", "yes", "y", "t")


def _dominant(rows: list[dict[str, str]], column: str, default: str = "") -> str:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(column, "")).strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return default
    return max(counts, key=lambda value: (counts[value], value))


def _confidence(value: Any, *, default: float = 1.0, denominator: float | None = None) -> float:
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
        # The {yyyymmdd} token occupies one whole path segment (the dated folder name).
        parts = rel.split("/")
        idx = next((i for i, p in enumerate(parts) if "{yyyymmdd}" in p), -1)
        if idx < 0:
            raise ValueError(f"dated file_path must contain {{yyyymmdd}}: {rel}")
        parent_dir = root.joinpath(*parts[:idx]) if idx else root
        seg_prefix, _, seg_suffix = parts[idx].partition("{yyyymmdd}")
        remainder = parts[idx + 1:]
        pattern = re.compile(rf"{re.escape(seg_prefix)}(\d{{8}}){re.escape(seg_suffix)}$")
        candidates: list[tuple[str, Path]] = []
        if parent_dir.exists():
            for child in parent_dir.iterdir():
                m = pattern.match(child.name)
                if not m:
                    continue
                target_file = child.joinpath(*remainder) if remainder else child
                if target_file.exists():
                    candidates.append((m.group(1), target_file.resolve()))
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
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("final_score"))
        if not ticker or native is None:
            continue
        rank_ready = _truthy(r.get("rank_ready_flag"))
        calib_ok = _truthy(r.get("calibration_eligible_flag"))
        complete = str(r.get("model_status", "")).strip().lower() in ("", "complete")
        eligible = rank_ready and calib_ok and complete
        reason = "ok" if eligible else (
            "not_rank_ready" if not rank_ready else
            "not_calibration_eligible" if not calib_ok else "model_incomplete"
        )
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=str(cfg["industry"]),
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            score_confidence=_confidence(r.get("data_quality_confidence")),
            source_asof_date=str(r.get("asof_date", "")).strip(),
        ))
    return out


def _adapt_biotech(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    score_field = _dominant(rows, "production_rank_score_field", default="opportunity_score")
    out: list[CanonicalScore] = []
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get(score_field))
        if not ticker or native is None:
            continue
        investible = _truthy(r.get("biotech_cohort_investible_flag"))
        vetoed = _truthy(r.get("core_structural_veto_flag"))
        eligible = investible and not vetoed
        reason = "ok" if eligible else ("core_structural_veto" if vetoed else "not_investible")
        industry = str(r.get("biotech_primary_cohort", "")).strip() or str(cfg["industry"])
        conf = _confidence(r.get("data_quality_confidence_multiplier"))
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=industry,
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            score_confidence=conf,
            source_asof_date=str(r.get("asof_date", "")).strip(),
        ))
    return out


def _adapt_med_devices(cfg: dict[str, Any], rows: list[dict[str, str]]) -> list[CanonicalScore]:
    out: list[CanonicalScore] = []
    for r in rows:
        ticker = str(r.get("ticker", "")).strip().upper()
        native = _f(r.get("composite_score"))
        if not ticker or native is None:
            continue
        eligible = _truthy(r.get("passed_tier1_safety_gate"))
        reason = "ok" if eligible else "failed_tier1_safety_gate"
        industry = str(r.get("subsector", "")).strip() or str(cfg["industry"])
        completeness = _f(r.get("data_completeness_score"))
        conf = _confidence(completeness, denominator=100.0)
        out.append(CanonicalScore(
            ticker=ticker, source_pipeline=str(cfg["model_family"]),
            sector=str(cfg["sector"]), industry=industry,
            industry_aggregate=str(cfg["industry_aggregate"]), native_score=native,
            investable_eligible=int(eligible), eligibility_reason=reason,
            score_confidence=conf, source_asof_date=str(r.get("asof_date", "")).strip(),
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
