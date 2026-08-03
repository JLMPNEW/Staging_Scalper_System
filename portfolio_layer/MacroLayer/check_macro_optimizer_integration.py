#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
OPTIMIZER_ROOT = REPO_ROOT / "optimizer"
if str(OPTIMIZER_ROOT) not in sys.path:
    sys.path.insert(0, str(OPTIMIZER_ROOT))

from macro_raw_config import (  # noqa: E402
    cfg_get,
    configure_pipeline_logging,
    load_macro_raw_config,
    parse_boolish,
    resolve_path,
)

logger = logging.getLogger(__name__)

from run_macro_optimizer_integration import (  # noqa: E402
    _case_config,
    _resolve_base_config as _resolve_accepted_base_config,
)

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_errors(case_dir: Path, *, case_name: str, expected_run_id: str) -> list[str]:
    manifest_path = case_dir / "stage12d_case_manifest.json"
    if not manifest_path.exists():
        return ["missing_case_manifest"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["invalid_case_manifest"]
    errors: list[str] = []
    if payload.get("status") != "completed":
        errors.append("manifest_status_not_completed")
    if payload.get("case_name") != case_name:
        errors.append("manifest_case_mismatch")
    if not expected_run_id or payload.get("run_id") != expected_run_id:
        errors.append("manifest_run_id_mismatch")
    output_hashes = payload.get("output_sha256")
    if not isinstance(output_hashes, dict) or not output_hashes:
        errors.append("missing_output_hashes")
    else:
        for filename, expected_hash in output_hashes.items():
            candidate = case_dir / str(filename)
            if not candidate.exists() or _sha256_file(candidate) != str(expected_hash):
                errors.append(f"output_hash_mismatch:{filename}")
    input_files = payload.get("input_files")
    if not isinstance(input_files, dict) or not input_files:
        errors.append("missing_input_hashes")
    else:
        for name, item in input_files.items():
            if not isinstance(item, dict):
                errors.append(f"invalid_input_manifest:{name}")
                continue
            candidate = Path(str(item.get("path") or ""))
            if not candidate.exists() or _sha256_file(candidate) != str(item.get("sha256") or ""):
                errors.append(f"input_hash_mismatch:{name}")
    return errors


def _summary_run_id(output_dir: Path, *, case_names: set[str]) -> str:
    summary_path = output_dir / "stage12d_optimizer_case_summary.csv"
    if not summary_path.exists():
        return ""
    summary = pd.read_csv(summary_path)
    if "run_id" not in summary.columns or "case_name" not in summary.columns:
        return ""
    selected = summary.loc[summary["case_name"].astype(str).isin(case_names)].copy()
    if set(selected["case_name"].astype(str)) != case_names:
        return ""
    run_ids = {str(value).strip() for value in selected["run_id"] if str(value).strip()}
    return next(iter(run_ids)) if len(run_ids) == 1 else ""

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
    expected_run_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    case_name = str(case.get("name", "")).strip()
    case_dir = output_dir / case_name
    weights_path = case_dir / "weights_long_only.csv"
    rows: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []

    manifest_errors = _manifest_errors(case_dir, case_name=case_name, expected_run_id=expected_run_id)
    rows.append({
        "run_id": expected_run_id,
        "case_name": case_name,
        "check_name": "same_run_manifest_and_hashes",
        "value": ";".join(manifest_errors),
        "threshold": "no errors",
        "passed": int(not manifest_errors),
    })
    exists = weights_path.exists()
    rows.append({"run_id": expected_run_id, "case_name": case_name, "check_name": "weights_long_only_exists", "value": int(exists), "threshold": "1", "passed": int(exists)})
    if not exists:
        return rows, breaches

    weights = pd.read_csv(weights_path)
    required_columns = {"Ticker", "Weight", "Sleeve"}
    schema_ok = required_columns.issubset(weights.columns)
    rows.append({"run_id": expected_run_id, "case_name": case_name, "check_name": "weights_schema", "value": sorted(weights.columns), "threshold": sorted(required_columns), "passed": int(schema_ok)})
    if not schema_ok:
        return rows, breaches
    numeric_weights = pd.to_numeric(weights["Weight"], errors="coerce")
    valid_weights = numeric_weights.notna().all() and bool(numeric_weights.ge(0.0).all())
    rows.append({"run_id": expected_run_id, "case_name": case_name, "check_name": "weights_finite_nonnegative", "value": int(valid_weights), "threshold": "1", "passed": int(valid_weights)})
    total_weight = float(numeric_weights.fillna(0.0).sum())
    tol = float(thresholds["weight_sum_tolerance"])
    rows.append({
        "run_id": expected_run_id,
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
            "run_id": expected_run_id,
            "case_name": case_name,
            "check_name": "foreign_budget_respected",
            "value": foreign_weight,
            "threshold": f"<={budget + breach_tol}",
            "passed": int(foreign_weight <= budget + breach_tol),
        })
        target_breaches = _target_breaches(weights, base_cfg, float(thresholds["max_target_band_breach"]))
        breaches.extend({"case_name": case_name, **b} for b in target_breaches)
        rows.append({
            "run_id": expected_run_id,
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
    base_config_path = _resolve_accepted_base_config(config_path, layer_cfg, args.base_config)
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

    case_names = {str(case.get("name", "")).strip() for case in cases}
    expected_run_id = _summary_run_id(output_dir, case_names=case_names)
    rows: list[dict[str, Any]] = []
    breaches: list[dict[str, Any]] = []
    for case in cases:
        case_name = str(case.get("name", "")).strip()
        case_cfg = _case_config(
            base_cfg,
            case,
            output_dir / case_name,
            base_config_path,
            layer_cfg,
            config_path,
        )
        case_rows, case_breaches = _check_case(
            case,
            output_dir=output_dir,
            base_cfg=case_cfg,
            thresholds=thresholds,
            expected_run_id=expected_run_id,
        )
        rows.extend(case_rows)
        breaches.extend(case_breaches)

    summary = pd.DataFrame(rows)
    breach_frame = pd.DataFrame(
        breaches,
        columns=["case_name", "target_type", "name", "actual", "max_weight"],
    )
    checks_dir = output_dir / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    acceptance_path = checks_dir / "stage12d_optimizer_acceptance_summary.csv"
    breach_path = checks_dir / "stage12d_target_breaches.csv"
    summary.to_csv(acceptance_path, index=False)
    breach_frame.to_csv(breach_path, index=False)
    passed = bool(expected_run_id) and (bool(summary["passed"].astype(int).eq(1).all()) if not summary.empty else False)
    case_summary_path = output_dir / "stage12d_optimizer_case_summary.csv"
    case_manifest_hashes = {
        case_name: _sha256_file(output_dir / case_name / "stage12d_case_manifest.json")
        for case_name in sorted(case_names)
        if (output_dir / case_name / "stage12d_case_manifest.json").exists()
    }
    acceptance_manifest = {
        "run_id": expected_run_id,
        "acceptance": "PASS" if passed else "FAIL",
        "case_names": sorted(case_names),
        "case_summary_sha256": _sha256_file(case_summary_path) if case_summary_path.exists() else "",
        "case_manifest_sha256": case_manifest_hashes,
        "files": {
            acceptance_path.name: _sha256_file(acceptance_path),
            breach_path.name: _sha256_file(breach_path),
        },
    }
    manifest_path = checks_dir / "stage12d_optimizer_acceptance_manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.tmp")
    temporary.write_text(json.dumps(acceptance_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    print(f"STAGE_12D_EXIT_GATE={'PASS' if passed else 'FAIL'}")
    logger.info("Stage 12D optimizer integration acceptance: %s", "PASS" if passed else "FAIL")
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
