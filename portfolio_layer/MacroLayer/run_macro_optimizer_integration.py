#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
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
    utc_now_iso,
)

logger = logging.getLogger(__name__)

_TIER1_IMPORT_ERROR: Exception | None = None
_TIER1: dict[str, Any] = {}
try:
    from tier1_common import _get_tier1_cfg as _tier1_get_cfg
    from tier1_portfolio_optimizer import _resolve_cfg_path as _tier1_resolve_cfg_path
    from tier1_portfolio_optimizer import load_yaml as _tier1_load_yaml
    from tier1_portfolio_optimizer import run_end_to_end_from_cfg as _tier1_run_end_to_end_from_cfg

    _TIER1.update(
        {
            "get_cfg": _tier1_get_cfg,
            "load_yaml": _tier1_load_yaml,
            "resolve_cfg_path": _tier1_resolve_cfg_path,
            "run_end_to_end_from_cfg": _tier1_run_end_to_end_from_cfg,
        }
    )
except ModuleNotFoundError as exc:
    _TIER1_IMPORT_ERROR = exc


def _require_tier1(name: str) -> Any:
    if name in _TIER1:
        return _TIER1[name]
    raise RuntimeError(
        "The copied MacroLayer no longer loads tier1 optimizer modules at import time. "
        "Stage 7 should consume the MacroLayer serving tables through the portfolio-layer optimizer adapter."
    ) from _TIER1_IMPORT_ERROR


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Stage 12D final optimizer macro/baseline cases.")
    parser.add_argument("--config", type=Path, default=Path("MacroLayer/config_macro_raw.yaml"))
    parser.add_argument("--base-config", type=Path, default=None, help="Optional tier1 config override.")
    parser.add_argument("--case", action="append", default=None, help="Run only the named case. Repeatable.")
    parser.add_argument("--force", action="store_true", help="Run even when optimizer_integration_layer.enabled is false.")
    return parser.parse_args()


def _resolve_output_dir(config_path: Path, layer_cfg: dict[str, Any]) -> Path:
    out_dir = resolve_path(config_path, str(layer_cfg.get("output_dir", "MacroLayer/out/final_optimizer")))
    if out_dir is None:
        raise ValueError("optimizer_integration_layer.output_dir could not be resolved.")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _resolve_base_config(config_path: Path, layer_cfg: dict[str, Any], override: Path | None) -> Path:
    if override is not None:
        p = override.expanduser()
        return p.resolve() if p.is_absolute() else (REPO_ROOT / p).resolve()
    explicit = str(layer_cfg.get("base_config_path", "") or "").strip()
    if explicit:
        p = resolve_path(config_path, explicit)
        if p is None:
            raise ValueError("optimizer_integration_layer.base_config_path could not be resolved.")
        return p

    pattern = str(
        layer_cfg.get("base_config_glob", "output/runs/*/blacklitterman/bl_optimizer_config.yaml")
    ).strip()
    candidates = sorted(REPO_ROOT.glob(pattern), reverse=True)
    rejected: list[str] = []
    for candidate in candidates:
        manifest_path = candidate.with_name("bl_manifest.json")
        if not manifest_path.exists():
            rejected.append(f"{candidate}:missing_manifest")
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rejected.append(f"{candidate}:invalid_manifest")
            continue
        if str(manifest.get("acceptance", "")).strip().upper() != "PASS":
            rejected.append(f"{candidate}:manifest_not_pass")
            continue
        return candidate.resolve()
    raise FileNotFoundError(
        f"No accepted sealed tier1 optimizer config matched {pattern!r}; rejected={rejected[-5:]}"
    )

def _set_nested(root: dict[str, Any], keys: list[str], value: Any) -> None:
    cur = root
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _pin_existing_input_paths(cfg: dict[str, Any], base_config_path: Path) -> None:
    paths = cfg.get("paths", {}) or {}
    if not isinstance(paths, dict):
        return
    resolve_cfg_path = _require_tier1("resolve_cfg_path")
    for key in ("stocks_scores_csv", "sector_rotation_csv", "foreign_etfs_csv", "ticker_company_csv", "user_portfolio_csv"):
        raw = paths.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            paths[key] = resolve_cfg_path(str(raw), cfg, base_config_path)
        except Exception:
            logger.warning("Unable to pre-resolve optimizer input path key=%s value=%s", key, raw)
    cfg["paths"] = paths


def _case_config(
    base_cfg: dict[str, Any],
    case: dict[str, Any],
    case_dir: Path,
    base_config_path: Path,
    layer_cfg: dict[str, Any],
    macro_config_path: Path,
) -> dict[str, Any]:
    cfg = copy.deepcopy(base_cfg)
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(cfg)
    _pin_existing_input_paths(tier1_cfg, base_config_path)
    macro_enabled = parse_boolish(case.get("macro_enabled"), default=False)
    foreign_enabled = parse_boolish(case.get("foreign_enabled"), default=True)
    long_short_enabled = parse_boolish(case.get("long_short_enabled"), default=False)

    macro_cfg = copy.deepcopy(dict(layer_cfg.get("adapter", {}) or {}))
    if macro_enabled and not macro_cfg:
        raise ValueError("optimizer_integration_layer.adapter is required for macro-enabled cases.")
    for section, keys in {
        "inputs": ("stock_csv", "foreign_csv"),
        "stock_targets": ("industry_targets_csv", "sector_targets_csv"),
        "foreign_sleeve": ("budget_csv", "candidates_csv"),
    }.items():
        section_cfg = macro_cfg.get(section, {}) or {}
        for key in keys:
            raw_path = str(section_cfg.get(key, "") or "").strip()
            if raw_path:
                resolved = resolve_path(macro_config_path, raw_path)
                section_cfg[key] = str(resolved) if resolved is not None else raw_path
        macro_cfg[section] = section_cfg
    macro_cfg["enabled"] = macro_enabled
    foreign_cfg = macro_cfg.get("foreign_sleeve", {}) or {}
    if not isinstance(foreign_cfg, dict):
        foreign_cfg = {}
    foreign_cfg["enabled"] = foreign_enabled
    macro_cfg["foreign_sleeve"] = foreign_cfg
    tier1_cfg["macro_optimizer_integration"] = macro_cfg

    _set_nested(tier1_cfg, ["optimization", "long_short", "enabled"], long_short_enabled)
    _set_nested(tier1_cfg, ["output", "out_dir"], str(case_dir))
    _set_nested(tier1_cfg, ["output", "write_weights_csvs"], True)
    return cfg


def _optimizer_window(cfg: dict[str, Any]) -> tuple[pd.Timestamp, pd.Timestamp]:
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(cfg)
    returns_cfg = dict(tier1_cfg.get("returns", {}) or {})
    start_raw = returns_cfg.get("start", tier1_cfg.get("start", None))
    end_raw = returns_cfg.get("end", tier1_cfg.get("end", None))
    if start_raw is None or str(start_raw).strip() == "":
        raise ValueError("Stage 12D price overlay requires returns.start or top-level start.")
    start = pd.to_datetime(start_raw, errors="coerce")
    if pd.isna(start):
        raise ValueError(f"Invalid Stage 12D returns.start: {start_raw!r}")
    end = pd.to_datetime(end_raw, errors="coerce") if end_raw is not None and str(end_raw).strip() else pd.Timestamp.today()
    if pd.isna(end):
        raise ValueError(f"Invalid Stage 12D returns.end: {end_raw!r}")
    return pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()


def _foreign_overlay_tickers(cfg: dict[str, Any]) -> list[str]:
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(cfg)
    try:
        import rotation_timeseries as rt
        universe = rt.parse_rotation_universe(tier1_cfg)
        tickers = list(universe.foreign_candidates)
    except Exception:
        tickers = []

    macro_cfg = dict(tier1_cfg.get("macro_optimizer_integration", {}) or {})
    foreign_cfg = dict(macro_cfg.get("foreign_sleeve", {}) or {})
    candidate_path = str(foreign_cfg.get("candidates_csv", "")).strip()
    if candidate_path:
        try:
            path = Path(candidate_path).expanduser()
            if not path.is_absolute():
                path = (REPO_ROOT / path).resolve()
            frame = pd.read_csv(path)
            if "ticker" in frame.columns:
                latest = frame
                if "as_of_date" in frame.columns:
                    dates = pd.to_datetime(frame["as_of_date"], errors="coerce")
                    if dates.notna().any():
                        latest = frame.loc[dates.eq(dates.max())].copy()
                if "selected_flag" in latest.columns:
                    selected = pd.to_numeric(latest["selected_flag"], errors="coerce").fillna(0).astype(int).eq(1)
                    latest = latest.loc[selected].copy()
                tickers.extend(latest["ticker"].astype(str).str.upper().str.strip().tolist())
        except Exception as exc:
            logger.warning("Unable to read Stage 12D foreign candidate tickers: %s", exc)

    out: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        t = str(ticker).upper().strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _build_price_overlay(cfg: dict[str, Any], layer_cfg: dict[str, Any], case_name: str) -> dict[str, pd.DataFrame] | None:
    get_tier1_cfg = _require_tier1("get_cfg")
    tier1_cfg = get_tier1_cfg(cfg)
    overlay_cfg = dict(layer_cfg.get("price_overlay", {}) or {})
    if not parse_boolish(overlay_cfg.get("enabled"), default=True):
        return None
    source = str(overlay_cfg.get("source", "ibkr_rotation")).strip().lower()
    if source not in {"ibkr_rotation", "none"}:
        raise ValueError("optimizer_integration_layer.price_overlay.source must be ibkr_rotation or none.")
    if source == "none":
        return None

    tickers = _foreign_overlay_tickers(tier1_cfg)
    if not tickers:
        return None

    start, end = _optimizer_window(tier1_cfg)
    lookback_raw = overlay_cfg.get("lookback_start", None)
    lookback_start = pd.to_datetime(lookback_raw, errors="coerce") if lookback_raw is not None and str(lookback_raw).strip() else start
    if pd.isna(lookback_start):
        lookback_start = start

    try:
        import rotation_timeseries as rt
        logger.info(
            "Loading Stage 12D price overlay via IBKR rotation path: case=%s tickers=%s range=%s..%s",
            case_name,
            tickers,
            pd.Timestamp(lookback_start).date(),
            end.date(),
        )
        data = rt.download_ohlcv_auto(
            tickers=tickers,
            start_inclusive=pd.Timestamp(lookback_start).normalize(),
            end_inclusive=end,
            cfg=tier1_cfg,
        )
        out = {
            str(k).upper(): v
            for k, v in (data or {}).items()
            if v is not None and not v.empty and "Close" in v.columns and np.isfinite(pd.to_numeric(v["Close"], errors="coerce")).any()
        }
        missing = sorted(set(tickers) - set(out))
        if missing:
            logger.warning("Stage 12D IBKR price overlay missing tickers: %s", missing)
        return out or None
    except Exception:
        if parse_boolish(overlay_cfg.get("fail_hard"), default=True):
            raise
        logger.exception("Stage 12D price overlay failed; continuing without overlay.")
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _output_snapshot(case_dir: Path, *, long_short_enabled: bool) -> dict[str, int | None]:
    names = ["weights_long_only.csv"]
    if long_short_enabled:
        names.append("weights_long_short.csv")
    return {
        name: ((case_dir / name).stat().st_mtime_ns if (case_dir / name).exists() else None)
        for name in names
    }


def _verify_fresh_outputs(case_dir: Path, before: dict[str, int | None], *, started_ns: int) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, previous_mtime in before.items():
        path = case_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Stage 12D did not produce required output: {path}")
        current_mtime = path.stat().st_mtime_ns
        if current_mtime < started_ns or (previous_mtime is not None and current_mtime <= previous_mtime):
            raise RuntimeError(f"Stage 12D output was not freshly written by this run: {path}")
        hashes[name] = _sha256_file(path)
    return hashes


def _input_hashes(
    *,
    cfg_case: dict[str, Any],
    base_config_path: Path,
    macro_config_path: Path,
) -> dict[str, dict[str, str]]:
    paths: dict[str, Path] = {
        "base_config": base_config_path,
        "macro_config": macro_config_path,
    }
    tier1_cfg = _require_tier1("get_cfg")(cfg_case)
    adapter = dict(tier1_cfg.get("macro_optimizer_integration", {}) or {})
    for section_name in ("inputs", "stock_targets", "foreign_sleeve"):
        section = dict(adapter.get(section_name, {}) or {})
        for key, value in section.items():
            if not str(key).endswith("_csv") or not str(value or "").strip():
                continue
            candidate = Path(str(value)).expanduser()
            if candidate.exists() and candidate.is_file():
                paths[f"{section_name}.{key}"] = candidate.resolve()
    return {
        name: {"path": str(path.resolve()), "sha256": _sha256_file(path)}
        for name, path in sorted(paths.items())
    }


def _write_case_manifest(
    *,
    path: Path,
    run_id: str,
    case_name: str,
    started_at_utc: str,
    output_hashes: dict[str, str],
    input_hashes: dict[str, dict[str, str]],
) -> None:
    payload = {
        "run_id": run_id,
        "case_name": case_name,
        "status": "completed",
        "started_at_utc": started_at_utc,
        "completed_at_utc": utc_now_iso(),
        "input_files": input_hashes,
        "output_sha256": output_hashes,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)

def _result_rows(case_name: str, results: dict[str, Any], case_dir: Path, run_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for portfolio_name, result in results.items():
        row: dict[str, Any] = {
            "run_id": run_id,
            "case_name": case_name,
            "portfolio": portfolio_name,
            "case_dir": str(case_dir),
        }
        row.update(dict(result.metrics))
        rows.append(row)
    return rows


def main() -> None:
    configure_pipeline_logging()
    args = parse_args()
    config_path, macro_cfg = load_macro_raw_config(args.config)
    layer_cfg = dict(cfg_get(macro_cfg, "optimizer_integration_layer", default={}) or {})
    if not parse_boolish(layer_cfg.get("enabled"), default=False) and not args.force:
        logger.info("Stage 12D optimizer integration is disabled; use --force for an explicit direct run.")
        print("STAGE_12D=DISABLED")
        return
    output_dir = _resolve_output_dir(config_path, layer_cfg)
    base_config_path = _resolve_base_config(config_path, layer_cfg, args.base_config)
    if not base_config_path.exists():
        raise FileNotFoundError(f"Base tier1 config not found: {base_config_path}")

    load_yaml = _require_tier1("load_yaml")
    run_end_to_end_from_cfg = _require_tier1("run_end_to_end_from_cfg")
    base_cfg = load_yaml(str(base_config_path))
    cases = list(layer_cfg.get("cases", []) or [])
    if not cases:
        cases = [{"name": "macro_full", "macro_enabled": True, "foreign_enabled": True, "long_short_enabled": False}]
    selected = set(args.case or [])
    if selected:
        cases = [c for c in cases if str(c.get("name", "")).strip() in selected]
    if not cases:
        raise ValueError("No Stage 12D cases selected.")

    run_id = uuid.uuid4().hex
    summary_rows: list[dict[str, Any]] = []
    failed: list[str] = []
    for case in cases:
        case_name = str(case.get("name", "")).strip()
        if not case_name:
            raise ValueError("optimizer_integration_layer.cases entries require a name.")
        case_dir = output_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Running Stage 12D optimizer case=%s output_dir=%s", case_name, case_dir)
        try:
            cfg_case = _case_config(
                base_cfg, case, case_dir, base_config_path, layer_cfg, config_path
            )
            long_short_enabled = parse_boolish(case.get("long_short_enabled"), default=False)
            before = _output_snapshot(case_dir, long_short_enabled=long_short_enabled)
            started_ns = time.time_ns()
            started_at_utc = utc_now_iso()
            price_overlay = _build_price_overlay(cfg_case, layer_cfg, case_name)
            results = run_end_to_end_from_cfg(cfg_case, cfg_path=base_config_path, prices_by_ticker=price_overlay)
            output_hashes = _verify_fresh_outputs(case_dir, before, started_ns=started_ns)
            input_hashes = _input_hashes(
                cfg_case=cfg_case,
                base_config_path=base_config_path,
                macro_config_path=config_path,
            )
            _write_case_manifest(
                path=case_dir / "stage12d_case_manifest.json",
                run_id=run_id,
                case_name=case_name,
                started_at_utc=started_at_utc,
                output_hashes=output_hashes,
                input_hashes=input_hashes,
            )
            summary_rows.extend(_result_rows(case_name, results, case_dir, run_id))
        except Exception:
            failed.append(case_name)
            logger.exception("Stage 12D optimizer case failed: %s", case_name)
            if parse_boolish(layer_cfg.get("fail_hard"), default=True):
                raise

    summary = pd.DataFrame(summary_rows)
    if not summary.empty:
        summary.insert(0, "updated_at_utc", utc_now_iso())
        summary.to_csv(output_dir / "stage12d_optimizer_case_summary.csv", index=False)
    if failed:
        raise RuntimeError(f"Stage 12D failed cases: {failed}")
    logger.info("Stage 12D optimizer integration complete: cases=%d output_dir=%s", len(cases), output_dir)


if __name__ == "__main__":
    main()
