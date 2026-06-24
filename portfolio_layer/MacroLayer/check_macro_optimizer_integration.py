#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    load_macro_raw_config,
    parse_boolish,
    resolve_path,
)

logger = logging.getLogger(__name__)

_TIER1_IMPORT_ERROR: Exception | None = None
_TIER1: dict[str, Any] = {}
try:
    from tier1_common import _get_tier1_cfg as _tier1_get_cfg
    from tier1_portfolio_optimizer import load_yaml as _tier1_load_yaml

    _TIER1.update({"get_cfg": _tier1_get_cfg, "load_yaml": _tier1_load_yaml})
except ModuleNotFoundError as exc:
    _TIER1_IMPORT_ERROR = exc


def _require_tier1(name: str) -> Any:
    if name in _TIER1:
        return _TIER1[name]
    raise RuntimeError(
        "The copied MacroLayer no longer loads tier1 optimizer modules at import time. "
        "Stage 7 should validate the optimizer contract through the portfolio-layer adapter."
    ) from _TIER1_IMPORT_ERROR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 12D optimizer integration acceptance checks.")
    parser.add_argument("--config", type=Path, default=Path("MacroLayer/config_macro_raw.yaml"))
    parser.add_argument("--base-config", type=Path, default=None, help="Optional tier1 config override.")
    parser.add_argument("--case", action="append", default=None, help="Check only the named case. Repeatable.")
    return parser.parse_args()


def _resolve_output_dir(config_path: Path, layer_cfg: dict[str, Any]) -> Path:
    out_dir = resolve_path(config_path, str(layer_cfg.get("output_dir", "MacroLayer/out/final_optimizer")))
    if out_dir is None:
        raise ValueError("optimizer_integration_layer.output_dir could not be resolved.")
    return out_dir


def _resolve_base_config(config_path: Path, layer_cfg: dict[str, Any], override: Path | None) -> Path:
    if override is not None:
        p = override.expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()
    raw = str(layer_cfg.get("base_config_path", "config.yaml")).strip() or "config.yaml"
    p = resolve_path(config_path, raw)
    if p is None:
        raise ValueError("optimizer_integration_layer.base_config_path could not be resolved.")
    return p


def _resolve_repo_path(raw_path: Any) -> Path:
    p = Path(str(raw_path)).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


def _latest_rows(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return pd.DataFrame(columns=df.columns)
    dates = pd.to_datetime(df[date_col], errors="coerce")
    if dates.notna().sum() == 0:
        return pd.DataFrame(columns=df.columns)
    return df.loc[dates.eq(dates.max())].copy()


def _latest_foreign_budget(base_cfg: dict[str, Any]) -> float:
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(base_cfg)
    macro_cfg = dict(tier1_cfg.get("macro_optimizer_integration", {}) or {})
    foreign_cfg = dict(macro_cfg.get("foreign_sleeve", {}) or {})
    budget_path = str(foreign_cfg.get("budget_csv", "")).strip()
    if not budget_path:
        return 0.0
    df = pd.read_csv(_resolve_repo_path(budget_path))
    latest = _latest_rows(df, "as_of_date")
    if latest.empty:
        return 0.0
    row = latest.iloc[0]
    active = parse_boolish(row.get("active_flag"), default=False)
    if not active:
        return 0.0
    return float(pd.to_numeric(pd.Series([row.get("foreign_budget", 0.0)]), errors="coerce").fillna(0.0).iloc[0])


def _target_breaches(weights: pd.DataFrame, base_cfg: dict[str, Any], tolerance: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(base_cfg)
    macro_cfg = dict(tier1_cfg.get("macro_optimizer_integration", {}) or {})
    target_cfg = dict(macro_cfg.get("stock_targets", {}) or {})
    if not parse_boolish(target_cfg.get("enabled"), default=False):
        return rows
    normalize_caps = parse_boolish(target_cfg.get("normalize_caps_to_available_groups"), default=True)
    max_weight_buffer = max(0.0, float(target_cfg.get("max_weight_buffer", 0.0)))

    us = weights.loc[weights["Sleeve"].astype(str).eq("US")].copy()
    us_total = float(pd.to_numeric(us["Weight"], errors="coerce").fillna(0.0).sum())
    if us.empty or us_total <= 0.0:
        return rows

    industry_path = str(target_cfg.get("industry_targets_csv", "")).strip()
    if industry_path and "IndustryName" in us.columns:
        targets = _latest_rows(pd.read_csv(_resolve_repo_path(industry_path)), "as_of_date")
        industry_names = us["IndustryName"].fillna("").astype(str).str.strip()
        represented = {name for name in industry_names.unique().tolist() if name}
        actual = pd.to_numeric(us["Weight"], errors="coerce").fillna(0.0).groupby(industry_names).sum() / us_total
        scale = 1.0
        if normalize_caps and not targets.empty:
            cap_sum = 0.0
            for _, row in targets.iterrows():
                name = str(row.get("industry_name", "")).strip()
                if name not in represented:
                    continue
                mx = pd.to_numeric(pd.Series([row.get("max_weight", None)]), errors="coerce").iloc[0]
                if pd.notna(mx):
                    cap_sum += max(0.0, float(mx))
            if 1e-12 < cap_sum < 1.0:
                scale = 1.0 / cap_sum
        for _, row in targets.iterrows():
            name = str(row.get("industry_name", "")).strip()
            if not name or name not in actual.index:
                continue
            mx = pd.to_numeric(pd.Series([row.get("max_weight", None)]), errors="coerce").iloc[0]
            max_allowed = float(mx) * scale + max_weight_buffer
            if pd.notna(mx) and float(actual.loc[name]) > max_allowed + tolerance:
                rows.append({"target_type": "industry", "name": name, "actual": float(actual.loc[name]), "max_weight": max_allowed})

    sector_path = str(target_cfg.get("sector_targets_csv", "")).strip()
    if sector_path and "SectorName" in us.columns:
        targets = _latest_rows(pd.read_csv(_resolve_repo_path(sector_path)), "as_of_date")
        sector_names = us["SectorName"].fillna("").astype(str).str.strip()
        represented = {name for name in sector_names.unique().tolist() if name}
        actual = pd.to_numeric(us["Weight"], errors="coerce").fillna(0.0).groupby(sector_names).sum() / us_total
        scale = 1.0
        if normalize_caps and not targets.empty:
            cap_sum = 0.0
            for _, row in targets.iterrows():
                name = str(row.get("sector_name", "")).strip()
                if name not in represented:
                    continue
                mx = pd.to_numeric(pd.Series([row.get("max_weight", None)]), errors="coerce").iloc[0]
                if pd.notna(mx):
                    cap_sum += max(0.0, float(mx))
            if 1e-12 < cap_sum < 1.0:
                scale = 1.0 / cap_sum
        for _, row in targets.iterrows():
            name = str(row.get("sector_name", "")).strip()
            if not name or name not in actual.index:
                continue
            mx = pd.to_numeric(pd.Series([row.get("max_weight", None)]), errors="coerce").iloc[0]
            max_allowed = float(mx) * scale + max_weight_buffer
            if pd.notna(mx) and float(actual.loc[name]) > max_allowed + tolerance:
                rows.append({"target_type": "sector", "name": name, "actual": float(actual.loc[name]), "max_weight": max_allowed})
    return rows


def _check_case(
    case: dict[str, Any],
    *,
    output_dir: Path,
    base_cfg: dict[str, Any],
    thresholds: dict[str, float],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    case_name = str(case.get("name", "")).strip()
    case_dir = output_dir / case_name
    weights_path = case_dir / "weights_long_only.csv"
    rows: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []

    exists = weights_path.exists()
    rows.append({"case_name": case_name, "check_name": "weights_long_only_exists", "value": int(exists), "threshold": "1", "passed": int(exists)})
    if not exists:
        return rows, breaches

    weights = pd.read_csv(weights_path)
    total_weight = float(pd.to_numeric(weights["Weight"], errors="coerce").fillna(0.0).sum())
    tol = float(thresholds["weight_sum_tolerance"])
    rows.append({
        "case_name": case_name,
        "check_name": "weight_sum",
        "value": total_weight,
        "threshold": f"1 +/- {tol}",
        "passed": int(abs(total_weight - 1.0) <= tol),
    })

    macro_enabled = parse_boolish(case.get("macro_enabled"), default=False)
    foreign_enabled = parse_boolish(case.get("foreign_enabled"), default=True)
    foreign_weight = float(
        pd.to_numeric(weights.loc[weights["Sleeve"].astype(str).eq("FOREIGN"), "Weight"], errors="coerce").fillna(0.0).sum()
    )
    if macro_enabled:
        budget = _latest_foreign_budget(base_cfg) if foreign_enabled else 0.0
        breach_tol = float(thresholds["max_foreign_budget_breach"])
        rows.append({
            "case_name": case_name,
            "check_name": "foreign_budget_respected",
            "value": foreign_weight,
            "threshold": f"<={budget + breach_tol}",
            "passed": int(foreign_weight <= budget + breach_tol),
        })
        target_breaches = _target_breaches(weights, base_cfg, float(thresholds["max_target_band_breach"]))
        breaches.extend({"case_name": case_name, **b} for b in target_breaches)
        rows.append({
            "case_name": case_name,
            "check_name": "stock_target_max_breaches",
            "value": len(target_breaches),
            "threshold": "0",
            "passed": int(len(target_breaches) == 0),
        })
    return rows, breaches


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, macro_cfg = load_macro_raw_config(args.config)
    layer_cfg = dict(cfg_get(macro_cfg, "optimizer_integration_layer", default={}) or {})
    output_dir = _resolve_output_dir(config_path, layer_cfg)
    base_config_path = _resolve_base_config(config_path, layer_cfg, args.base_config)
    load_yaml = _require_tier1("load_yaml")
    base_cfg = load_yaml(str(base_config_path))
    cases = list(layer_cfg.get("cases", []) or [])
    selected = set(args.case or [])
    if selected:
        cases = [c for c in cases if str(c.get("name", "")).strip() in selected]
    if not cases:
        raise ValueError("No Stage 12D cases selected.")

    acceptance = dict(layer_cfg.get("acceptance", {}) or {})
    thresholds = {
        "weight_sum_tolerance": float(acceptance.get("weight_sum_tolerance", 1e-6)),
        "max_foreign_budget_breach": float(acceptance.get("max_foreign_budget_breach", 1e-6)),
        "max_target_band_breach": float(acceptance.get("max_target_band_breach", 1e-6)),
    }

    rows: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []
    for case in cases:
        case_rows, case_breaches = _check_case(case, output_dir=output_dir, base_cfg=base_cfg, thresholds=thresholds)
        rows.extend(case_rows)
        breaches.extend(case_breaches)

    summary = pd.DataFrame(rows)
    breach_frame = pd.DataFrame(
        breaches,
        columns=["case_name", "target_type", "name", "actual", "max_weight"],
    )
    checks_dir = output_dir / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    summary.to_csv(checks_dir / "stage12d_optimizer_acceptance_summary.csv", index=False)
    breach_frame.to_csv(checks_dir / "stage12d_target_breaches.csv", index=False)
    passed = bool(summary["passed"].astype(int).eq(1).all()) if not summary.empty else False
    print(f"STAGE_12D_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    logger.info("Stage 12D optimizer integration acceptance: %s", "PASS" if passed else "FAIL")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
