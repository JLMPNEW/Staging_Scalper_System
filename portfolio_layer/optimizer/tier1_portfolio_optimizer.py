#!/usr/bin/env python3
"""
Tier-1 / Institutional-grade Portfolio Construction & Optimization

Implements:
- Universe selection (cardinality enforced via YAML-driven pre-selection)
- Black-Litterman expected returns (equilibrium prior + rating/sector/foreign views)
- Risk: Pearson + Kendall tau covariance scenarios, shrinkage, PSD fix
- Optimization: long-only, long/short, optional user-portfolio optimization
- Output: weights + low/high bands derived from Pearson/Kendall (+ optional bootstraps)

Inputs (CSV):
1) stocks_scores_csv must include columns:
   Ticker, sector, industry, industry_aggregate, Rating, FinalScore

2) sector_rotation_csv must include columns:
   SectorName, ScorePct, State  (Ticker optional)

3) foreign_etfs_csv must include columns:
   Ticker, MarketName, Score, ScorePct, State

4) user_portfolio_csv (optional) must include at least:
   Ticker, Weight   (weights can be missing -> treated as 0)

Dependencies:
  pip install pandas numpy scipy cvxpy pyyaml
Optional:
  pip install scikit-learn yfinance ib_insync

Run:
  python tier1_portfolio_optimizer.py --config config.yaml
"""

from __future__ import annotations

import argparse
import numbers
import logging
import math
import os
import copy
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

import numpy as np
import pandas as pd
from portfolio_layer.optimizer.tier1_common import _get_tier1_cfg

# Risk / clustering
from scipy.cluster.hierarchy import linkage, fcluster
from scipy.spatial.distance import squareform

# Optimization
try:
    # NOTE:
    # - If you're seeing Pylance "reportMissingImports", the fix is to install cvxpy in
    #   your interpreter env: `pip install cvxpy`
    # - This try/except keeps the module importable even when cvxpy isn't installed.
    import cvxpy as cp  # pyright: ignore[reportMissingImports]
except Exception:  # pragma: no cover
    cp = cast(Any, None)

# Optional shrinkage helpers
try:
    from sklearn.covariance import LedoitWolf, OAS
except Exception:
    LedoitWolf = None
    OAS = None

# Optional data
try:
    import yfinance as yf
except Exception:
    yf = None


def _atomic_dataframe_csv(frame: Any, path: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    os.close(fd)
    try:
        frame.to_csv(tmp_name, index=False)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.remove(tmp_name)


# --------------------------
# Logging
# --------------------------
logger = logging.getLogger("tier1_opt")


# --------------------------
# Utilities
# --------------------------
def load_yaml(path: str) -> Dict[str, Any]:
    import yaml  # local import so script can be imported without PyYAML
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _opt_str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    if s == "" or s.lower() in ("none", "null"):
        return None
    return s


def _get_cache_update_tickers(base_tickers: List[str], cache_cfg: Dict[str, Any]) -> List[str]:
    """
    Expand cache updates with helper benchmark tickers while keeping downstream price
    loads limited to the originally requested asset list.
    """
    extra_raw = cache_cfg.get("extra_tickers", [])
    if extra_raw in (None, ""):
        extra_list: List[object] = []
    elif isinstance(extra_raw, str):
        extra_list = [x.strip() for x in extra_raw.split(",") if x.strip()]
    elif isinstance(extra_raw, list):
        extra_list = extra_raw
    else:
        raise ValueError("config.yaml: ohlcv_cache.extra_tickers must be a YAML list or comma-separated string.")

    merged: List[str] = []
    seen: set[str] = set()
    for t in list(base_tickers) + [str(x).strip().upper() for x in extra_list if str(x).strip()]:
        t_u = str(t).strip().upper()
        if not t_u or t_u in seen:
            continue
        seen.add(t_u)
        merged.append(t_u)
    return merged


def _resolve_output_dir(cfg: Dict[str, Any], cfg_path: Optional[Path]) -> Optional[Path]:
    out_cfg = cfg.get("output", {}) or {}
    out_raw = out_cfg.get("out_dir", None)
    if out_raw is None or str(out_raw).strip() == "":
        out_raw = cfg.get("output_dir", None)
    if out_raw is None or str(out_raw).strip() == "":
        return None
    out_dir = Path(str(out_raw)).expanduser()
    if not out_dir.is_absolute() and cfg_path is not None:
        out_dir = (cfg_path.parent / out_dir).resolve()
    return out_dir


def _resolve_cfg_path(path: str, cfg: Dict[str, Any], cfg_path: Optional[Path]) -> str:
    p = Path(str(path))
    if p.is_absolute():
        return str(p)
    out_dir = _resolve_output_dir(cfg, cfg_path)
    # Prefer output-dir copy when present (pipeline artifacts are written there).
    if out_dir is not None:
        out_candidate = (out_dir / p).resolve()
        if out_candidate.exists():
            return str(out_candidate)
    # Then prefer config-directory copy when present.
    if cfg_path is not None:
        cfg_candidate = (cfg_path.parent / p).resolve()
        if cfg_candidate.exists():
            return str(cfg_candidate)
    # Production runs often version output files as name_YYYYMMDD.csv while configs keep
    # stable logical names like sector_rotation_latest.csv.
    if out_dir is not None and p.suffix:
        patterns = [f"{p.stem}_*{p.suffix}", f"{p.stem}*{p.suffix}"]
        matches: List[Path] = []
        for pattern in patterns:
            matches.extend([x.resolve() for x in out_dir.glob(pattern) if x.is_file()])
            matches.extend([x.resolve() for x in out_dir.glob(f"*/{pattern}") if x.is_file()])
        if matches:
            latest = max(dict.fromkeys(matches), key=lambda x: x.stat().st_mtime)
            logger.info("Resolved %s to latest versioned artifact %s.", path, latest)
            return str(latest)
    if out_dir is not None:
        return str((out_dir / p).resolve())
    if cfg_path is not None:
        return str((cfg_path.parent / p).resolve())
    return str(p.resolve())


def _uses_precomputed_covariance(cfg: Dict[str, Any]) -> bool:
    rcfg = cfg.get("risk", {}) or {}
    source = str(rcfg.get("covariance_source", "")).strip().lower()
    raw_path = rcfg.get("covariance_csv", (cfg.get("paths", {}) or {}).get("covariance_csv", ""))
    return source in {"stage2_covariance_csv", "precomputed_csv", "csv"} and str(raw_path).strip() != ""


def zscore(x: pd.Series) -> pd.Series:
    x = x.astype(float)
    if len(x) == 0:
        return pd.Series(dtype="float64", index=x.index)
    mu = float(x.mean())
    sd = float(x.std(ddof=1))
    if not math.isfinite(sd) or sd <= 1e-12:
        logger.warning("zscore: near-constant series (std=%.3e); returning zeros.", sd)
        return pd.Series(np.zeros(len(x)), index=x.index)
    return (x - mu) / sd


class OptimizationInfeasibleError(RuntimeError):
    def __init__(self, portfolio_name: str, status: str):
        self.portfolio_name = str(portfolio_name)
        self.status = str(status)
        super().__init__(f"Optimization infeasible for {self.portfolio_name}. Status: {self.status}")


def clip_series(x: pd.Series, lo: float, hi: float) -> pd.Series:
    return x.clip(lower=lo, upper=hi)


def periods_per_year(freq: str) -> int:
    f = (freq or "").upper()
    if f.startswith("D"):
        return 252
    if f.startswith("W"):
        return 52
    if f.startswith("M"):
        return 12
    if f.startswith("Q"):
        return 4
    return 252


def annual_to_period_rate(r_annual: float, ppy: int) -> float:
    # geometric conversion (safe for non-small rates)
    return (1.0 + float(r_annual)) ** (1.0 / float(ppy)) - 1.0


def symmetrize(a: np.ndarray) -> np.ndarray:
    return 0.5 * (a + a.T)


def nearest_psd_cov(cov: np.ndarray, eig_floor: float = 1e-8) -> np.ndarray:
    """
    Practical PSD fix:
    - Symmetrize
    - Eigen-decompose
    - Clip eigenvalues to eig_floor
    - Reconstruct
    This is not the full Higham alternating projections, but is robust and fast.
    """
    cov = symmetrize(cov)
    vals, vecs = np.linalg.eigh(cov)
    vals = np.maximum(vals, eig_floor)
    cov_psd = (vecs * vals) @ vecs.T
    return symmetrize(cov_psd)


def _safe_cond(a: np.ndarray) -> float:
    try:
        return float(np.linalg.cond(a))
    except Exception:
        return float("inf")


def _is_cov_well_conditioned(cov: np.ndarray, max_cond: float) -> bool:
    if not np.isfinite(cov).all():
        return False
    cond = _safe_cond(cov)
    return bool(math.isfinite(cond) and cond <= float(max_cond))


def _stabilize_covariance(
    cov: np.ndarray,
    *,
    eig_floor: float,
    max_cond: float,
    name: str,
) -> np.ndarray:
    cov = symmetrize(cov)
    if not np.isfinite(cov).all():
        logger.warning("%s covariance has non-finite values; replacing with 0.0 and PSD-fixing.", name)
        cov = np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0)
    cov = nearest_psd_cov(cov, eig_floor)
    cond = _safe_cond(cov)
    if not math.isfinite(cond) or cond > float(max_cond):
        avg_var = float(np.mean(np.diag(cov)))
        jitter = max(float(eig_floor), avg_var * 1e-4)
        cov = cov + jitter * np.eye(cov.shape[0])
        cov = nearest_psd_cov(cov, eig_floor)
        cond2 = _safe_cond(cov)
        if not math.isfinite(cond2) or cond2 > float(max_cond):
            logger.warning("%s covariance remains ill-conditioned (cond=%.2e).", name, cond2)
        else:
            logger.warning("%s covariance stabilized with jitter (cond=%.2e).", name, cond2)
    return cov


def kendall_to_pearson(tau: np.ndarray) -> np.ndarray:
    # rho = sin(pi/2 * tau)
    return np.sin(0.5 * math.pi * tau)


def winsorize_df(df: pd.DataFrame, lower_q: float, upper_q: float) -> pd.DataFrame:
    out = df.copy()
    for c in out.columns:
        lo = out[c].quantile(lower_q)
        hi = out[c].quantile(upper_q)
        out[c] = out[c].clip(lower=lo, upper=hi)
    return out


def block_bootstrap_indices(n: int, block_size: int, rng: np.random.Generator) -> np.ndarray:
    """
    Simple block bootstrap for time series.
    """
    if n <= 0:
        return np.array([], dtype=int)
    if block_size <= 1:
        return rng.integers(0, n, size=n)
    if block_size >= n:
        logger.warning(
            "block_bootstrap: block_size=%d >= n=%d; falling back to IID sampling because "
            "block samples would otherwise be non-random.",
            block_size,
            n,
        )
        return rng.integers(0, n, size=n)
    idx = []
    while len(idx) < n:
        start = int(rng.integers(0, max(1, n - block_size + 1)))
        idx.extend(range(start, min(n, start + block_size)))
    return np.array(idx[:n], dtype=int)


# --------------------------
# Data loading
# --------------------------
def load_stocks_scores(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"Ticker", "sector", "Rating", "FinalScore"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"stocks_scores_csv missing columns: {missing}")
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    df["Rating"] = df["Rating"].astype(str).str.strip()
    df["sector"] = df["sector"].astype(str).str.strip()
    df["FinalScore"] = pd.to_numeric(df["FinalScore"], errors="coerce")
    for c in ("Ticker", "Rating", "sector"):
        df[c] = df[c].replace("", np.nan)
    bad_mask = df[["Ticker", "FinalScore", "Rating", "sector"]].isna().any(axis=1)
    if bool(bad_mask.any()):
        dropped = int(bad_mask.sum())
        sample = df.loc[bad_mask, "Ticker"].dropna().astype(str).head(10).tolist()
        logger.warning(
            "Dropping %d rows from stocks_scores due to missing required fields "
            "(Ticker/FinalScore/Rating/sector). Sample tickers: %s",
            dropped,
            ", ".join(sample) if sample else "<none>",
        )
    df = df.dropna(subset=["Ticker", "FinalScore", "Rating", "sector"])
    return df


def _coerce_bool_series(values: pd.Series, *, default: bool) -> pd.Series:
    if values.dtype == bool:
        return values.fillna(default).astype(bool)
    return values.map(lambda v: _as_bool(v, default)).astype(bool)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            return bool(default)
        return bool(int(value))
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "t"}:
        return True
    if text in {"0", "false", "no", "n", "f", "", "none", "null", "nan"}:
        return False
    return bool(default)


def _apply_stocks_universe_filters(stocks: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    out = stocks.copy()

    if "BaseOptimizerEligible" in out.columns:
        eligible = _coerce_bool_series(out["BaseOptimizerEligible"], default=True)
        blocked = int((~eligible).sum())
        if blocked > 0:
            logger.info("Applying BaseOptimizerEligible filter: excluding %d rows.", blocked)
        out = out.loc[eligible].copy()

    opt_cfg = cfg.get("optimization", {}) or {}
    ef_cfg = opt_cfg.get("earnings_filter", {}) or {}
    mode = str(ef_cfg.get("mode", "all")).strip().lower()
    if mode not in {"all", "exclude_upcoming", "both"}:
        logger.warning("Unknown earnings_filter.mode=%r; defaulting to 'all'.", mode)
        mode = "all"

    if mode == "both":
        logger.warning(
            "earnings_filter.mode='both' has no effect when running the optimizer directly; "
            "use main.py orchestration to produce separate all-stocks and exclude-upcoming runs. "
            "Falling back to no earnings filter (equivalent to 'all')."
        )

    if mode == "exclude_upcoming":
        if "EarningsBlocked_7D" not in out.columns:
            logger.warning(
                "earnings_filter.mode='exclude_upcoming' but EarningsBlocked_7D column was not found; "
                "no earnings filter applied. Ensure the input was built via the canonical optimizer universe step."
            )
        else:
            blocked = _coerce_bool_series(out["EarningsBlocked_7D"], default=False)
            n_blocked = int(blocked.sum())
            if n_blocked > 0:
                logger.info("Applying EarningsBlocked_7D filter: excluding %d rows.", n_blocked)
            out = out.loc[~blocked].copy()

    if out.empty:
        raise ValueError("No stocks remain after applying optimizer universe filters.")

    return out


def load_sector_rotation(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"SectorName", "ScorePct", "State"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"sector_rotation_csv missing columns: {missing}")
    df["SectorName"] = df["SectorName"].astype(str).str.strip()
    df["ScorePct"] = pd.to_numeric(df["ScorePct"], errors="coerce")
    df["State"] = df["State"].astype(str).str.strip()
    df = df.dropna(subset=["SectorName", "ScorePct", "State"])
    dup_mask = df["SectorName"].duplicated(keep="first")
    if dup_mask.any():
        dup_names = df.loc[dup_mask, "SectorName"].astype(str).tolist()
        logger.warning(
            "sector_rotation_csv has duplicate SectorName entries; keeping first: %s",
            dup_names,
        )
        df = df.drop_duplicates(subset=["SectorName"], keep="first")
    return df


def load_foreign_etfs(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"Ticker", "MarketName", "Score", "ScorePct", "State"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"foreign_etfs_csv missing columns: {missing}")
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    df["MarketName"] = df["MarketName"].astype(str).str.strip()
    df["Score"] = pd.to_numeric(df["Score"], errors="coerce")
    df["ScorePct"] = pd.to_numeric(df["ScorePct"], errors="coerce")
    df["State"] = df["State"].astype(str).str.strip()
    df = df.dropna(subset=["Ticker", "MarketName", "Score", "ScorePct", "State"])
    return df


def _latest_rows_by_date(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
    if df.empty or date_col not in df.columns:
        return pd.DataFrame(columns=df.columns)
    dates = pd.to_datetime(df[date_col], errors="coerce")
    if dates.notna().sum() == 0:
        return pd.DataFrame(columns=df.columns)
    latest = dates.max()
    return df.loc[dates.eq(latest)].copy()


def _resolve_stage12d_path(raw_path: Any, cfg: Dict[str, Any], cfg_path: Optional[Path]) -> Path:
    if raw_path is None or str(raw_path).strip() == "":
        raise ValueError("Stage 12D path is blank.")
    return Path(_resolve_cfg_path(str(raw_path), cfg, cfg_path)).expanduser().resolve()


def _load_stage12d_csv(raw_path: Any, cfg: Dict[str, Any], cfg_path: Optional[Path]) -> pd.DataFrame:
    path = _resolve_stage12d_path(raw_path, cfg, cfg_path)
    if not path.exists():
        raise FileNotFoundError(f"Stage 12D required file not found: {path}")
    return pd.read_csv(path)


def _set_nested_dict_value(root: Dict[str, Any], keys: List[str], value: Any) -> None:
    cur = root
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _stage12d_latest_foreign_budget(
    cfg: Dict[str, Any],
    cfg_path: Optional[Path],
    integration_cfg: Dict[str, Any],
) -> Tuple[float, pd.DataFrame]:
    foreign_cfg = integration_cfg.get("foreign_sleeve", {}) or {}
    budget_path = foreign_cfg.get("budget_csv", "")
    candidates_path = foreign_cfg.get("candidates_csv", "")
    if str(budget_path).strip() == "":
        return float("nan"), pd.DataFrame()

    budget_df = _load_stage12d_csv(budget_path, cfg, cfg_path)
    budget_latest = _latest_rows_by_date(budget_df, "as_of_date")
    if budget_latest.empty:
        raise ValueError("Stage 12D foreign budget CSV has no dated rows.")
    b = budget_latest.iloc[0]
    active = _as_bool(b.get("active_flag", 0), default=False)
    foreign_budget = float(pd.to_numeric(pd.Series([b.get("foreign_budget", 0.0)]), errors="coerce").fillna(0.0).iloc[0])
    if not active:
        foreign_budget = 0.0

    candidates = pd.DataFrame()
    if str(candidates_path).strip() != "":
        candidates = _load_stage12d_csv(candidates_path, cfg, cfg_path)
        candidates = _latest_rows_by_date(candidates, "as_of_date")
        if not candidates.empty:
            candidates["ticker"] = candidates["ticker"].astype(str).str.upper().str.strip()
            if "selected_flag" in candidates.columns:
                selected = pd.to_numeric(candidates["selected_flag"], errors="coerce").fillna(0).astype(int).eq(1)
                candidates = candidates.loc[selected].copy()
    return max(0.0, foreign_budget), candidates


def _load_stage12d_targets(
    cfg: Dict[str, Any],
    cfg_path: Optional[Path],
    integration_cfg: Dict[str, Any],
) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {}
    target_cfg = integration_cfg.get("stock_targets", {}) or {}
    if not bool(target_cfg.get("enabled", False)):
        return out

    industry_path = target_cfg.get("industry_targets_csv", "")
    if str(industry_path).strip() != "":
        industry = _load_stage12d_csv(industry_path, cfg, cfg_path)
        industry = _latest_rows_by_date(industry, "as_of_date")
        if not industry.empty:
            for c in ("sector_name", "industry_aggregate_name", "industry_name"):
                if c in industry.columns:
                    industry[c] = industry[c].fillna("").astype(str).str.strip()
            out["industry"] = industry

    sector_path = target_cfg.get("sector_targets_csv", "")
    if str(sector_path).strip() != "":
        sector = _load_stage12d_csv(sector_path, cfg, cfg_path)
        sector = _latest_rows_by_date(sector, "as_of_date")
        if not sector.empty:
            if "sector_name" in sector.columns:
                sector["sector_name"] = sector["sector_name"].fillna("").astype(str).str.strip()
            out["sector"] = sector
    return out


def _apply_macro_optimizer_integration(cfg: Dict[str, Any], cfg_path: Optional[Path]) -> None:
    """
    Stage 12D adapter.

    When enabled, the optimizer consumes Stage 12A macro-aware stock/foreign
    inputs and applies Stage 12B/12C portfolio construction controls without
    overwriting the user's source CSVs.
    """
    integration_cfg = cfg.get("macro_optimizer_integration", {}) or {}
    if not bool(integration_cfg.get("enabled", False)):
        return

    paths = cfg.get("paths", {}) or {}
    if not isinstance(paths, dict):
        raise ValueError("paths must be a mapping/dict when macro_optimizer_integration.enabled=true.")

    input_cfg = integration_cfg.get("inputs", {}) or {}
    stock_inputs = input_cfg.get("stock_csv", "")
    foreign_inputs = input_cfg.get("foreign_csv", "")
    if str(stock_inputs).strip() != "":
        paths["stocks_scores_csv"] = str(stock_inputs)
    if str(foreign_inputs).strip() != "":
        paths["foreign_etfs_csv"] = str(foreign_inputs)
    cfg["paths"] = paths

    targets = _load_stage12d_targets(cfg, cfg_path, integration_cfg)
    cfg["_stage12d_targets"] = targets
    if "sector" in targets and bool((integration_cfg.get("stock_targets", {}) or {}).get("use_sector_targets_as_benchmark", True)):
        sector_targets = targets["sector"]
        if {"sector_name", "target_weight"}.issubset(sector_targets.columns):
            weights = {
                str(r["sector_name"]): float(r["target_weight"])
                for _, r in sector_targets.iterrows()
                if str(r.get("sector_name", "")).strip() != "" and pd.notna(r.get("target_weight"))
            }
            if weights:
                sector_cfg = cfg.get("sector", {}) or {}
                if not isinstance(sector_cfg, dict):
                    sector_cfg = {}
                sector_cfg["benchmark_sector_weights"] = weights
                cfg["sector"] = sector_cfg

    foreign_cfg = integration_cfg.get("foreign_sleeve", {}) or {}
    foreign_enabled = bool(foreign_cfg.get("enabled", True))
    foreign_budget = 0.0
    candidates = pd.DataFrame()
    if foreign_enabled:
        foreign_budget, candidates = _stage12d_latest_foreign_budget(cfg, cfg_path, integration_cfg)
    cfg["_stage12d_foreign_budget"] = float(foreign_budget)
    cfg["_stage12d_selected_foreign_tickers"] = (
        set(candidates["ticker"].astype(str).str.upper().str.strip().tolist()) if not candidates.empty else set()
    )

    budget_mode = str(foreign_cfg.get("budget_mode", "max")).strip().lower()
    if budget_mode not in {"max", "target"}:
        raise ValueError("macro_optimizer_integration.foreign_sleeve.budget_mode must be max or target.")
    budget_buffer = max(0.0, float(foreign_cfg.get("budget_buffer", 0.0)))
    if not foreign_enabled:
        foreign_budget = 0.0
    foreign_max = min(1.0, max(0.0, foreign_budget + budget_buffer))
    foreign_min = foreign_max if budget_mode == "target" and foreign_max > 0.0 else 0.0

    _set_nested_dict_value(cfg, ["allocation", "region_budgets", "FOREIGN", "min"], foreign_min)
    _set_nested_dict_value(cfg, ["allocation", "region_budgets", "FOREIGN", "max"], foreign_max)
    _set_nested_dict_value(cfg, ["allocation", "long_short", "region_budgets", "FOREIGN", "min"], foreign_min)
    _set_nested_dict_value(cfg, ["allocation", "long_short", "region_budgets", "FOREIGN", "max"], foreign_max)

    if not foreign_enabled or foreign_max <= 0.0:
        _set_nested_dict_value(cfg, ["universe", "max_foreign_etfs"], 0)
    elif not candidates.empty:
        _set_nested_dict_value(cfg, ["universe", "max_foreign_etfs"], int(len(candidates)))
        if "portfolio_weight_at_budget" in candidates.columns:
            per_etf_cap = float(pd.to_numeric(candidates["portfolio_weight_at_budget"], errors="coerce").fillna(0.0).max())
            per_etf_cap = min(1.0, max(0.0, per_etf_cap + float(foreign_cfg.get("per_etf_cap_buffer", 0.0))))
            if per_etf_cap > 0.0:
                _set_nested_dict_value(cfg, ["optimization", "long_only", "max_weight_per_foreign_etf"], per_etf_cap)
                _set_nested_dict_value(cfg, ["optimization", "long_short", "max_weight_per_foreign_etf"], per_etf_cap)

    cfg.setdefault("universe", {})
    if isinstance(cfg["universe"], dict):
        cfg["universe"]["allowed_foreign_states"] = list(foreign_cfg.get("allowed_states", ["Eligible"]))


def _filter_stage12d_foreign_universe(foreign: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    integration_cfg = cfg.get("macro_optimizer_integration", {}) or {}
    if not bool(integration_cfg.get("enabled", False)):
        return foreign
    foreign_cfg = integration_cfg.get("foreign_sleeve", {}) or {}
    if not bool(foreign_cfg.get("enabled", True)) or float(cfg.get("_stage12d_foreign_budget", 0.0)) <= 0.0:
        out = foreign.copy()
        if "State" in out.columns:
            out["State"] = "Avoid"
        return out
    if bool(foreign_cfg.get("restrict_to_selected_candidates", True)):
        selected = set(cfg.get("_stage12d_selected_foreign_tickers", set()) or set())
        if selected:
            return foreign[foreign["Ticker"].astype(str).str.upper().str.strip().isin(selected)].copy()
    return foreign


def load_user_portfolio(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"Ticker", "Weight"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"user_portfolio_csv missing columns: {missing}")
    df["Ticker"] = df["Ticker"].astype(str).str.upper().str.strip()
    df["Weight"] = pd.to_numeric(df["Weight"], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["Ticker"])
    dup_mask = df["Ticker"].duplicated(keep=False)
    if dup_mask.any():
        dupes = sorted(df.loc[dup_mask, "Ticker"].unique().tolist())
        logger.warning(
            "User portfolio has duplicate tickers; keeping first occurrence and ignoring the rest. "
            "Duplicates: %s",
            ", ".join(dupes),
        )
        df = df.drop_duplicates(subset=["Ticker"], keep="first")
    total_w = float(df["Weight"].sum())
    if not np.isfinite(total_w):
        logger.warning("User portfolio weights sum to a non-finite value; check input data.")
    elif total_w <= 0.0:
        logger.warning("User portfolio weights sum to %.6f; expected positive weights.", total_w)
    elif total_w > 1.05:
        logger.warning(
            "User portfolio weights sum to %.6f (>1.0). If weights are in percent, scale to 0-1.",
            total_w,
        )
    return df[["Ticker", "Weight"]]


def load_ticker_company_map(path: str) -> Dict[str, str]:
    try:
        df = pd.read_csv(path)
    except Exception as e:
        logger.warning("Failed to read ticker_company_csv at %s (%s).", path, str(e))
        return {}

    if df is None or df.empty:
        return {}

    col_map = {str(c).strip().lower(): str(c).strip() for c in df.columns}
    ticker_col = col_map.get("ticker") or col_map.get("symbol")
    name_col = col_map.get("company") or col_map.get("name") or col_map.get("security")
    if ticker_col is None or name_col is None:
        logger.warning("ticker_company_csv missing required columns: Ticker + Company/Name.")
        return {}

    sub = df[[ticker_col, name_col]].copy()
    sub[ticker_col] = sub[ticker_col].astype(str).str.upper().str.strip()
    sub[name_col] = sub[name_col].astype(str).str.strip()

    mapping: Dict[str, str] = {}
    for t, name in zip(sub[ticker_col], sub[name_col]):
        if not t or not name:
            continue
        if t in mapping:
            continue
        mapping[t] = name
    return mapping


# --------------------------
# Universe Selection
# --------------------------
RATING_ORDER = ["Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"]


def select_us_long_only(stocks: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    ucfg = _require_cfg_section(cfg, "universe")
    blcfg = cfg.get("black_litterman", {}) or {}
    if bool(ucfg.get("include_all_stocks_long_only", False)):
        out = stocks.copy()
        out["Sleeve"] = "US"
        return out

    max_n = int(ucfg.get("max_us_stocks_long_only", 0))
    quotas: Dict[str, int] = ucfg.get("per_rating_quota_long_only", {}) or {}

    # Pre-compute full-universe z-score if configured
    zscore_scope = str(blcfg.get("signal_zscore_scope", "selected")).lower()
    if zscore_scope == "full" and "FinalScore" in stocks.columns:
        stocks = stocks.copy()
        stocks["_FullUniverseZ"] = zscore(stocks["FinalScore"].astype(float))

    # Keep only ratings mentioned in quotas (or default: SB/Buy/Hold)
    allowed = set(quotas.keys()) if quotas else {"Strong Buy", "Buy", "Hold"}
    df = stocks[stocks["Rating"].isin(allowed)].copy()

    # Warn if quotas sum exceeds max_n
    if quotas and sum(int(v) for v in quotas.values()) > max_n:
        logger.warning(
            "per_rating_quota_long_only sums to %d > max_us_stocks_long_only=%d. "
            "Selection will be truncated to max_n in rating order.",
            sum(int(v) for v in quotas.values()),
            max_n,
        )

    selected = []
    for r in RATING_ORDER:
        k = int(quotas.get(r, 0))
        if k <= 0:
            continue
        remaining_n = max_n - len(selected)
        if remaining_n <= 0:
            break
        sub = df[df["Rating"] == r].sort_values("FinalScore", ascending=False)
        take = min(k, remaining_n)
        selected.extend(sub.head(take)["Ticker"].tolist())

    selected = list(dict.fromkeys(selected))  # de-dupe preserving order

    if len(selected) < max_n:
        remaining = df[~df["Ticker"].isin(selected)].sort_values("FinalScore", ascending=False)
        fill_tickers = remaining.head(max_n - len(selected))["Ticker"].tolist()
        if quotas and fill_tickers:
            logger.warning(
                "per_rating_quota_long_only fill-up added %d stocks beyond explicit quota counts "
                "to reach max_us_stocks_long_only=%d.",
                len(fill_tickers),
                max_n,
            )
        selected.extend(fill_tickers)

    out = df[df["Ticker"].isin(selected)].copy()
    out["Sleeve"] = "US"
    return out


def select_us_long_short(stocks: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    ucfg = _require_cfg_section(cfg, "universe")
    blcfg = cfg.get("black_litterman", {}) or {}
    max_longs = int(ucfg.get("max_us_longs_long_short", 0))
    max_shorts = int(ucfg.get("max_us_shorts_long_short", 0))
    q_longs: Dict[str, int] = ucfg.get("per_rating_quota_long_short_longs", {}) or {}
    q_shorts: Dict[str, int] = ucfg.get("per_rating_quota_long_short_shorts", {}) or {}

    longs_allowed = set(q_longs.keys()) if q_longs else {"Strong Buy", "Buy"}
    shorts_allowed = set(q_shorts.keys()) if q_shorts else {"Strong Sell", "Sell"}

    # Pre-compute full-universe z-score if configured
    zscore_scope = str(blcfg.get("signal_zscore_scope", "selected")).lower()
    if zscore_scope == "full" and "FinalScore" in stocks.columns:
        stocks = stocks.copy()
        stocks["_FullUniverseZ"] = zscore(stocks["FinalScore"].astype(float))

    df = stocks.copy()

    if q_longs and sum(int(v) for v in q_longs.values()) > max_longs:
        logger.warning(
            "per_rating_quota_long_short_longs sums to %d > max_us_longs_long_short=%d. "
            "Selection will be truncated to max_longs in rating order.",
            sum(int(v) for v in q_longs.values()),
            max_longs,
        )
    if q_shorts and sum(int(v) for v in q_shorts.values()) > max_shorts:
        logger.warning(
            "per_rating_quota_long_short_shorts sums to %d > max_us_shorts_long_short=%d. "
            "Selection will be truncated to max_shorts in rating order.",
            sum(int(v) for v in q_shorts.values()),
            max_shorts,
        )

    # Long book: best scores within positive ratings
    long_sel = []
    for r in RATING_ORDER:
        k = int(q_longs.get(r, 0))
        if k <= 0:
            continue
        sub = df[df["Rating"] == r].sort_values("FinalScore", ascending=False)
        long_sel.extend(sub.head(k)["Ticker"].tolist())
    long_sel = list(dict.fromkeys(long_sel))

    # Short book: "best shorts" = lowest scores within negative ratings
    short_sel = []
    for r in RATING_ORDER[::-1]:
        k = int(q_shorts.get(r, 0))
        if k <= 0:
            continue
        sub = df[df["Rating"] == r].sort_values("FinalScore", ascending=True)
        short_sel.extend(sub.head(k)["Ticker"].tolist())
    short_sel = list(dict.fromkeys(short_sel))

    long_df = df[
        df["Rating"].isin(longs_allowed)
        & ~df["Ticker"].isin(long_sel)
        & ~df["Ticker"].isin(short_sel)
    ].sort_values("FinalScore", ascending=False)
    long_sel.extend(long_df.head(max(0, max_longs - len(long_sel)))["Ticker"].tolist())
    long_sel = long_sel[:max_longs]

    short_df = df[
        df["Rating"].isin(shorts_allowed)
        & ~df["Ticker"].isin(short_sel)
        & ~df["Ticker"].isin(long_sel)
    ].sort_values("FinalScore", ascending=True)
    short_sel.extend(short_df.head(max(0, max_shorts - len(short_sel)))["Ticker"].tolist())
    short_sel = short_sel[:max_shorts]

    selected = list(dict.fromkeys(long_sel + short_sel))
    long_set = set(long_sel)
    short_set = set(short_sel)
    out = df[df["Ticker"].isin(selected)].copy()
    out["Sleeve"] = "US"
    out["LS_Book"] = out["Ticker"].map(lambda t: "LONG" if t in long_set else ("SHORT" if t in short_set else "NA"))
    return out


def select_foreign_etfs(foreign: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    ucfg = _require_cfg_section(cfg, "universe")
    max_n = int(ucfg.get("max_foreign_etfs", 0))
    allowed_states = set(ucfg.get("allowed_foreign_states", ["Eligible"]))

    df = foreign[foreign["State"].isin(allowed_states)].copy()
    df = df.sort_values("Score", ascending=False).head(max_n).copy()
    df["Sleeve"] = "FOREIGN"

    # Optional region grouping
    region_map = (cfg.get("allocation", {}) or {}).get("foreign_region_map", {}) or {}
    df["RegionGroup"] = df["MarketName"].map(lambda x: region_map.get(str(x), str(x)))

    return df


# --------------------------
# Returns Data Provider
# --------------------------
def _extract_close_series(df: pd.DataFrame) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    if "Close" in df.columns:
        s = df["Close"]
    elif "Adj Close" in df.columns:
        s = df["Adj Close"]
    else:
        return None

    s = pd.to_numeric(s, errors="coerce")
    if not isinstance(s.index, pd.DatetimeIndex):
        s.index = pd.to_datetime(s.index, errors="coerce")
    idx = pd.DatetimeIndex(s.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    s = s.copy()
    s.index = idx.normalize()
    return s.sort_index()


class ReturnsDataProvider:
    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        raise NotImplementedError

    def get_returns(
        self,
        tickers: List[str],
        start: str,
        end: Optional[str],
        freq: str,
        log_returns: bool,
    ) -> pd.DataFrame:
        prices: pd.DataFrame = self.get_prices(tickers, start, end)
        prices = prices.sort_index()
        # Resample to freq (last price)
        prices = prices.resample(freq).last().dropna(how="all")
        rets: pd.DataFrame
        if log_returns:
            # Keep this as a pandas DataFrame (Pyright/Pylance otherwise treats np.log(...)
            # as returning an ndarray and then flags .dropna as invalid).
            rets = (prices / prices.shift(1)).apply(np.log)
        else:
            rets = prices.pct_change()
        rets = rets.dropna(how="all")
        return rets


class YahooFinanceReturnsProvider(ReturnsDataProvider):
    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        if yf is None:
            raise RuntimeError("yfinance not installed. Install: pip install yfinance")
        data = yf.download(
            tickers=tickers,
            start=start,
            end=end,
            auto_adjust=True,
            progress=False,
            group_by="column",
            threads=True,
        )

        # yfinance stubs type this as Optional[...] in some environments; guard & narrow.
        if data is None or data.empty:
            raise ValueError("yfinance returned no data (empty response). Check tickers/date range.")

        px: pd.DataFrame
        # yfinance returns MultiIndex columns for multiple tickers
        if isinstance(data.columns, pd.MultiIndex):
            lvl0 = data.columns.get_level_values(0)
            if "Close" in lvl0:
                px_raw = data["Close"]
            elif "Adj Close" in lvl0:
                px_raw = data["Adj Close"]
            else:
                raise ValueError("yfinance response missing 'Close'/'Adj Close' columns.")

            # For some 1-ticker cases, this can be a Series; normalize to DataFrame.
            if isinstance(px_raw, pd.Series):
                px = px_raw.to_frame(name=tickers[0])
            else:
                px = px_raw.copy()
        else:
            # Single ticker: extract Close/Adj Close and name the column as the ticker.
            s = _extract_close_series(data)
            if s is None or s.empty:
                raise ValueError("yfinance response missing usable 'Close'/'Adj Close' series.")
            px = s.to_frame(name=tickers[0])

        px = px.dropna(how="all")

        # Ensure columns are exactly requested tickers (and ordered)
        cols = [t for t in tickers if t in px.columns]
        if not cols:
            raise ValueError("No requested tickers found in yfinance response columns.")
        return px.loc[:, cols].copy()


class CSVPricesReturnsProvider(ReturnsDataProvider):
    """
    Expects a CSV where first column is Date and remaining columns are tickers (prices).
    """
    def __init__(self, prices_csv: str):
        self.prices_csv = prices_csv

    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        df = pd.read_csv(self.prices_csv, parse_dates=[0])
        df = df.rename(columns={df.columns[0]: "Date"}).set_index("Date")
        idx = pd.DatetimeIndex(df.index).normalize()
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        df.index = idx
        # filter dates
        df = df.loc[df.index >= pd.to_datetime(start)]
        if end is not None:
            df = df.loc[df.index <= pd.to_datetime(end)]
        # keep tickers available
        cols = [t for t in tickers if t in df.columns]
        if not cols:
            raise ValueError("No requested tickers found in prices_csv.")
        return df[cols].copy()


class ParquetPricesReturnsProvider(ReturnsDataProvider):
    def __init__(
        self,
        *,
        cache_path: str,
        cache_cfg: Dict[str, Any],
        root_cfg: Dict[str, Any],
        update_on_use: bool = False,
    ):
        self.cache_path = str(cache_path)
        self.cache_cfg = cache_cfg or {}
        self.root_cfg = root_cfg or {}
        self.update_on_use = bool(update_on_use)

    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        try:
            from ohlcv_parquet_cache import (
                update_ohlcv_parquet_cache,
                load_close_prices_from_cache,
                parse_date,
            )
        except Exception as e:
            raise RuntimeError(
                "returns.use_ohlcv_cache=True but ohlcv_parquet_cache.py could not be imported."
            ) from e

        start_dt = pd.to_datetime(start).normalize()
        end_dt = pd.to_datetime(end).normalize() if end is not None else pd.Timestamp.today().normalize()

        if self.update_on_use:
            cache_start = parse_date(self.cache_cfg.get("start_date", None), default=start_dt)
            if cache_start is None:
                cache_start = start_dt

            data_source = str(
                self.cache_cfg.get("data_source", self.root_cfg.get("data_source", "yfinance"))
            ).strip().lower()
            batch_size = int(self.cache_cfg.get("batch_size", 200))
            partition = str(self.cache_cfg.get("partition", "year_month")).strip().lower()
            market_calendar = str(self.cache_cfg.get("market_calendar", "XNYS"))

            cache_update_tickers = _get_cache_update_tickers(tickers, self.cache_cfg)

            update_ohlcv_parquet_cache(
                cache_path=self.cache_path,
                tickers=cache_update_tickers,
                start_date=cache_start,
                end_date=end_dt,
                data_source=data_source,
                yfinance_cfg=self.root_cfg.get("yfinance", {}) or {},
                ibkr_cfg=self.root_cfg.get("ibkr", {}) or {},
                batch_size=batch_size,
                partition=partition,
                market_calendar=market_calendar,
            )

        return load_close_prices_from_cache(
            self.cache_path,
            tickers=tickers,
            start=start_dt,
            end=end_dt,
        )


class CachedPricesReturnsProvider(ReturnsDataProvider):
    def __init__(self, raw_by_ticker: Dict[str, pd.DataFrame]):
        self.raw_by_ticker = {str(k).upper(): v for k, v in (raw_by_ticker or {}).items()}

    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        start_dt = pd.to_datetime(start).normalize()
        end_dt = pd.to_datetime(end).normalize() if end is not None else None

        frames: Dict[str, pd.Series] = {}
        missing: List[str] = []
        for t in tickers:
            t_u = str(t).upper()
            df = self.raw_by_ticker.get(t_u)
            s = _extract_close_series(df) if df is not None else None
            if s is None or s.empty:
                missing.append(t_u)
                continue
            if end_dt is not None:
                s = s.loc[s.index <= end_dt]
            s = s.loc[s.index >= start_dt]
            frames[t_u] = s

        if missing:
            logger.warning("Missing cached prices for tickers: %s", missing)
        if not frames:
            raise ValueError("No cached prices available for requested tickers.")
        return pd.DataFrame(frames).sort_index()


class OverlayPricesReturnsProvider(ReturnsDataProvider):
    """
    Use explicit in-memory OHLCV for selected tickers, then fall back to the
    configured provider for everything else.

    This mirrors main.py behavior where rotation IBKR OHLCV can be merged into
    the optimizer without forcing all stock prices to come from the same source.
    """
    def __init__(self, raw_by_ticker: Dict[str, pd.DataFrame], fallback: ReturnsDataProvider):
        self.raw_by_ticker = {str(k).upper(): v for k, v in (raw_by_ticker or {}).items()}
        self.fallback = fallback

    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        start_dt = pd.to_datetime(start).normalize()
        end_dt = pd.to_datetime(end).normalize() if end is not None else None

        frames: Dict[str, pd.Series] = {}
        missing: List[str] = []
        for t in tickers:
            t_u = str(t).upper()
            df = self.raw_by_ticker.get(t_u)
            s = _extract_close_series(df) if df is not None else None
            if s is None or s.empty:
                missing.append(t_u)
                continue
            if end_dt is not None:
                s = s.loc[s.index <= end_dt]
            s = s.loc[s.index >= start_dt]
            if s.empty:
                missing.append(t_u)
                continue
            frames[t_u] = s

        if missing:
            try:
                fallback_prices = self.fallback.get_prices(missing, start, end)
                for col in fallback_prices.columns:
                    frames[str(col).upper()] = fallback_prices[col]
            except Exception:
                if not frames:
                    raise
                logger.warning("Fallback price provider could not load missing tickers: %s", missing)

        if not frames:
            raise ValueError("No prices available from overlay or fallback provider.")
        return pd.DataFrame(frames).sort_index()


class IBKRReturnsProvider(CachedPricesReturnsProvider):
    def __init__(
        self,
        ibkr_cfg: Optional[Dict[str, Any]] = None,
        raw_by_ticker: Optional[Dict[str, pd.DataFrame]] = None,
        fetch_missing: bool = True,
    ):
        super().__init__(raw_by_ticker or {})
        self.ibkr_cfg = ibkr_cfg or {}
        self.fetch_missing = bool(fetch_missing)

    def _download_missing(self, tickers: List[str], start_dt: pd.Timestamp, end_dt: pd.Timestamp) -> None:
        try:
            from ibkr_data import download_ohlc_ibkr  # type: ignore
        except ModuleNotFoundError as e:
            raise ModuleNotFoundError(
                "returns.source is 'ibkr' but ibkr_data.py could not be imported. "
                "Ensure ibkr_data.py is on PYTHONPATH and ib_insync is installed."
            ) from e

        data = download_ohlc_ibkr(
            tickers=tickers,
            start_inclusive=start_dt,
            end_inclusive=end_dt,
            ibkr_cfg=self.ibkr_cfg,
        )
        for t, df in data.items():
            self.raw_by_ticker[str(t).upper()] = df

    def get_prices(self, tickers: List[str], start: str, end: Optional[str]) -> pd.DataFrame:
        tickers_u = [str(t).upper() for t in tickers]
        missing = [t for t in tickers_u if t not in self.raw_by_ticker]

        if missing and self.fetch_missing:
            start_dt = pd.to_datetime(start).normalize()
            end_dt = pd.to_datetime(end).normalize() if end is not None else pd.Timestamp.today().normalize()
            self._download_missing(missing, start_dt, end_dt)

        return super().get_prices(tickers_u, start, end)


def _require_cfg_section(cfg: Dict[str, Any], key: str) -> Dict[str, Any]:
    section = cfg.get(key, None)
    if not isinstance(section, dict):
        raise ValueError(f"Missing or invalid '{key}' section in config.")
    return section


def _get_returns_window(cfg: Dict[str, Any], root_cfg: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    rc = _require_cfg_section(cfg, "returns")
    start = _opt_str(rc.get("start", root_cfg.get("start", None)))
    if start is None:
        raise ValueError("returns.start is required (or set top-level start).")
    end = _opt_str(rc.get("end", root_cfg.get("end", None)))
    return start, end


def _build_returns_provider(
    *,
    cfg: Dict[str, Any],
    root_cfg: Dict[str, Any],
    prices_by_ticker: Optional[Dict[str, pd.DataFrame]] = None,
    provider: Optional[ReturnsDataProvider] = None,
    cfg_path: Optional[Path] = None,
) -> ReturnsDataProvider:
    if provider is not None:
        return provider

    rc = _require_cfg_section(cfg, "returns")
    source = str(rc.get("source", "yfinance")).strip().lower()

    def _fallback_provider() -> ReturnsDataProvider:
        use_cache = bool(rc.get("use_ohlcv_cache", False))
        if use_cache:
            cache_cfg = root_cfg.get("ohlcv_cache", cfg.get("ohlcv_cache", {})) or {}
            cache_path_raw = rc.get("ohlcv_cache_path", cache_cfg.get("path", None))
            if cache_path_raw is None or str(cache_path_raw).strip() == "":
                raise ValueError("returns.use_ohlcv_cache is True but ohlcv_cache.path is missing.")
            cache_path = _resolve_cfg_path(str(cache_path_raw), root_cfg, cfg_path)
            update_on_use = bool(rc.get("ohlcv_cache_update", False))
            return ParquetPricesReturnsProvider(
                cache_path=cache_path,
                cache_cfg=cache_cfg,
                root_cfg=root_cfg,
                update_on_use=update_on_use,
            )

        if source == "csv":
            prices_csv = rc.get("prices_csv", None)
            if prices_csv is None or str(prices_csv).strip() == "":
                raise ValueError("returns.prices_csv is required when returns.source is 'csv'.")
            return CSVPricesReturnsProvider(str(prices_csv))

        if source in {"ibkr", "ib", "interactivebrokers", "interactive_brokers"}:
            ibkr_cfg = root_cfg.get("ibkr", cfg.get("ibkr", {})) or {}
            return IBKRReturnsProvider(ibkr_cfg=ibkr_cfg)

        return YahooFinanceReturnsProvider()

    if prices_by_ticker is not None and source in {"ibkr", "ib", "interactivebrokers", "interactive_brokers"}:
        ibkr_cfg = root_cfg.get("ibkr", cfg.get("ibkr", {})) or {}
        fetch_missing = bool(rc.get("ibkr_fetch_missing", True))
        return IBKRReturnsProvider(
            ibkr_cfg=ibkr_cfg,
            raw_by_ticker=prices_by_ticker,
            fetch_missing=fetch_missing,
        )

    if prices_by_ticker is not None:
        return OverlayPricesReturnsProvider(prices_by_ticker, _fallback_provider())

    return _fallback_provider()


# --------------------------
# Risk Model (Pearson + Kendall) + shrinkage + scenarios
# --------------------------
@dataclass
class CovScenario:
    name: str
    cov: np.ndarray


def shrink_covariance(
    returns: np.ndarray,
    method: str,
    manual_delta: float = 0.2
) -> Tuple[np.ndarray, float]:
    """
    Returns (shrunk_cov, shrink_delta_used).
    For sklearn LW/OAS, returns their implied shrinkage_.
    For manual, uses manual_delta with scaled-identity target.
    """
    method = (method or "manual").lower()
    x = returns
    # sample covariance
    S = np.cov(x, rowvar=False, ddof=1)

    if method == "ledoit_wolf":
        if LedoitWolf is None:
            logger.warning("scikit-learn not available; falling back to manual shrinkage.")
            method = "manual"
        else:
            lw = LedoitWolf().fit(x)
            return symmetrize(lw.covariance_), float(lw.shrinkage_)
    if method == "oas":
        if OAS is None:
            logger.warning("scikit-learn not available; falling back to manual shrinkage.")
            method = "manual"
        else:
            oas = OAS().fit(x)
            return symmetrize(oas.covariance_), float(oas.shrinkage_)

    # manual shrinkage to scaled identity
    delta = float(manual_delta)
    avg_var = float(np.mean(np.diag(S)))
    T = avg_var * np.eye(S.shape[0])
    cov = delta * T + (1.0 - delta) * S
    return symmetrize(cov), delta


def covariance_from_corr(corr: np.ndarray, vols: np.ndarray) -> np.ndarray:
    D = np.diag(vols)
    return D @ corr @ D


def _build_kendall_covariance(rets: pd.DataFrame, *, kendall_manual_delta: float) -> np.ndarray:
    tau = rets.corr(method="kendall").values
    rho_k = kendall_to_pearson(tau)
    np.fill_diagonal(rho_k, 1.0)
    vols = rets.std(ddof=1).to_numpy(dtype=float)
    cov_raw = covariance_from_corr(rho_k, vols)
    avg_var = float(np.mean(np.diag(cov_raw)))
    target = avg_var * np.eye(cov_raw.shape[0])
    return symmetrize(kendall_manual_delta * target + (1.0 - kendall_manual_delta) * cov_raw)


def _blend_covariance(base_cov: np.ndarray, stress_cov: Optional[np.ndarray], *, weight: float) -> np.ndarray:
    if stress_cov is None:
        return symmetrize(base_cov)
    w = float(np.clip(float(weight), 0.0, 1.0))
    if w <= 0.0:
        return symmetrize(base_cov)
    return symmetrize((1.0 - w) * base_cov + w * stress_cov)


def build_cov_scenarios(
    rets: pd.DataFrame,
    cfg: Dict[str, Any],
    rng: np.random.Generator,
    include_bootstrap: bool = True
) -> List[CovScenario]:
    rcfg = _require_cfg_section(cfg, "risk")
    precomputed = _load_precomputed_covariance_for_returns(rets, cfg)
    if precomputed is not None:
        return precomputed

    scenarios_cfg = (rcfg.get("scenarios", {}) or {})
    shrink_method = rcfg.get("shrinkage", "manual")
    manual_delta = float(rcfg.get("manual_shrink_delta", 0.2))
    kendall_manual_delta = float(np.clip(rcfg.get("kendall_manual_shrink_delta", manual_delta), 0.0, 1.0))
    eig_floor = float(rcfg.get("psd_eigen_floor", 1e-8))
    max_cond = float(rcfg.get("max_cov_condition", 1e12))

    X = rets.values
    # Pearson base (shrunk)
    covP, _ = shrink_covariance(X, shrink_method, manual_delta)
    covK = _build_kendall_covariance(rets, kendall_manual_delta=kendall_manual_delta)

    shock_cfg = (scenarios_cfg.get("shock", {}) or {})
    shock_enabled = bool(shock_cfg.get("enabled", False))
    shock_blend_weight = float(np.clip(shock_cfg.get("blend_weight", 0.35), 0.0, 1.0))
    shock_max_blend_weight = float(
        np.clip(shock_cfg.get("max_blend_weight", max(shock_blend_weight, 0.75)), 0.0, 1.0)
    )
    shock_vol_lookback = max(5, int(shock_cfg.get("vol_lookback_days", 20)))
    shock_min_obs = max(10, int(shock_cfg.get("min_obs", 40)))
    shock_quantile = float(np.clip(shock_cfg.get("stress_quantile", 0.90), 0.50, 0.99))
    shock_covP: Optional[np.ndarray] = None
    shock_covK: Optional[np.ndarray] = None
    shock_weight_effective = float(shock_blend_weight)

    if shock_enabled and len(rets) >= max(shock_min_obs, shock_vol_lookback):
        proxy = pd.to_numeric(rets.mean(axis=1), errors="coerce").dropna()
        rolling_vol = proxy.rolling(
            shock_vol_lookback,
            min_periods=max(5, shock_vol_lookback // 2),
        ).std(ddof=1)
        stress_mask = rolling_vol >= float(rolling_vol.quantile(shock_quantile))
        stress_idx = rolling_vol.index[stress_mask.fillna(False)]
        if len(stress_idx) < shock_min_obs:
            abs_proxy = proxy.abs()
            abs_thr = float(abs_proxy.quantile(shock_quantile))
            stress_idx = abs_proxy.index[abs_proxy >= abs_thr]
        stress_rets = rets.loc[rets.index.isin(stress_idx)].copy()
        if len(stress_rets) >= shock_min_obs:
            shock_covP, _ = shrink_covariance(stress_rets.values, shrink_method, manual_delta)
            shock_covK = _build_kendall_covariance(stress_rets, kendall_manual_delta=kendall_manual_delta)

            guard_cfg = (shock_cfg.get("realized_forecast_guard", {}) or {})
            if bool(guard_cfg.get("enabled", False)):
                guard_lookback = max(5, int(guard_cfg.get("lookback_days", 20)))
                trigger_ratio = max(1.0, float(guard_cfg.get("trigger_ratio", 1.5)))
                proxy_recent = proxy.tail(guard_lookback)
                realized_vol = float(proxy_recent.std(ddof=1)) if len(proxy_recent) >= 2 else np.nan
                n_assets = int(covP.shape[0])
                w_eq = np.repeat(1.0 / float(max(1, n_assets)), max(1, n_assets))
                forecast_var = float(w_eq @ covP @ w_eq)
                forecast_vol = float(np.sqrt(max(forecast_var, 0.0))) if np.isfinite(forecast_var) else np.nan
                if np.isfinite(realized_vol) and np.isfinite(forecast_vol) and forecast_vol > 0.0:
                    ratio = float(realized_vol / forecast_vol)
                    if ratio > trigger_ratio:
                        shock_weight_effective = min(
                            shock_max_blend_weight,
                            max(shock_blend_weight, shock_blend_weight * (ratio / trigger_ratio)),
                        )
                        logger.warning(
                            "Risk forecast guard triggered: realized universe vol %.4f vs forecast %.4f (ratio=%.2f). "
                            "Increasing shock covariance blend weight to %.2f.",
                            realized_vol,
                            forecast_vol,
                            ratio,
                            shock_weight_effective,
                        )
            covP = _blend_covariance(covP, shock_covP, weight=shock_weight_effective)
            covK = _blend_covariance(covK, shock_covK, weight=shock_weight_effective)
            logger.info(
                "Using shock-adjusted covariance blend: stress_obs=%d blend_weight=%.2f",
                int(len(stress_rets)),
                shock_weight_effective,
            )

    covP = _stabilize_covariance(covP, eig_floor=eig_floor, max_cond=max_cond, name="Pearson")
    covK = _stabilize_covariance(covK, eig_floor=eig_floor, max_cond=max_cond, name="Kendall")

    scenarios: List[CovScenario] = [
        CovScenario("Pearson", covP),
        CovScenario("Kendall", covK),
    ]

    # Optional bootstrap scenarios (for bands)
    boot_cfg = (scenarios_cfg.get("bootstrap", {}) or {})
    if include_bootstrap and bool(boot_cfg.get("enabled", False)):
        n_boot = int(boot_cfg.get("n_boot", 20))
        block = int(boot_cfg.get("block_size", 8))
        n = X.shape[0]
        for b in range(n_boot):
            idx = block_bootstrap_indices(n=n, block_size=block, rng=rng)
            Xb = X[idx, :]

            covPb, _ = shrink_covariance(Xb, shrink_method, manual_delta)
            dfb = pd.DataFrame(Xb, columns=rets.columns)
            covKb = _build_kendall_covariance(dfb, kendall_manual_delta=kendall_manual_delta)
            if shock_covP is not None and shock_weight_effective > 0.0:
                covPb = _blend_covariance(covPb, shock_covP, weight=shock_weight_effective)
            if shock_covK is not None and shock_weight_effective > 0.0:
                covKb = _blend_covariance(covKb, shock_covK, weight=shock_weight_effective)
            covPb = _stabilize_covariance(
                covPb,
                eig_floor=eig_floor,
                max_cond=max_cond,
                name=f"Pearson_boot{b+1:02d}",
            )
            covKb = _stabilize_covariance(
                covKb,
                eig_floor=eig_floor,
                max_cond=max_cond,
                name=f"Kendall_boot{b+1:02d}",
            )

            if (not _is_cov_well_conditioned(covPb, max_cond)) or (not _is_cov_well_conditioned(covKb, max_cond)):
                logger.warning("Skipping bootstrap scenario %d due to ill-conditioned covariance.", b + 1)
                continue

            scenarios.append(CovScenario(f"Pearson_boot{b+1:02d}", covPb))
            scenarios.append(CovScenario(f"Kendall_boot{b+1:02d}", covKb))

    return scenarios


def _load_precomputed_covariance_for_returns(rets: pd.DataFrame, cfg: Dict[str, Any]) -> Optional[List[CovScenario]]:
    if not _uses_precomputed_covariance(cfg):
        return None
    rcfg = _require_cfg_section(cfg, "risk")
    cfg_path = _cfg_path_from_cfg(cfg)
    raw_path = rcfg.get("covariance_csv", (cfg.get("paths", {}) or {}).get("covariance_csv", ""))
    cov_path = _resolve_cfg_path(str(raw_path), cfg, cfg_path)
    cov_df = pd.read_csv(cov_path, index_col=0)
    cov_df.index = cov_df.index.astype(str).str.upper().str.strip()
    cov_df.columns = cov_df.columns.astype(str).str.upper().str.strip()

    tickers = [str(c).upper().strip() for c in rets.columns.tolist()]
    if not tickers:
        raise ValueError("Precomputed covariance requested, but returns/assets ticker order is empty.")
    missing = sorted(set(tickers) - set(cov_df.index)) + sorted(set(tickers) - set(cov_df.columns))
    if missing:
        raise ValueError(f"Precomputed covariance missing optimizer tickers: {sorted(set(missing))[:20]}")

    cov = cov_df.loc[tickers, tickers].to_numpy(dtype=float)
    if not np.isfinite(cov).all():
        raise ValueError("Precomputed covariance contains non-finite values after ticker alignment.")
    cov = symmetrize(cov)

    units = str(rcfg.get("covariance_units", "annualized")).strip().lower()
    if units in {"annual", "annualized", "per_year", "yearly"}:
        ppy = periods_per_year(str((_require_cfg_section(cfg, "returns")).get("frequency", "D")))
        cov = cov / float(ppy)
    elif units not in {"period", "per_period", "daily", "weekly", "monthly"}:
        raise ValueError(f"Unknown risk.covariance_units={units!r} for precomputed covariance.")

    eig_floor = float(rcfg.get("psd_eigen_floor", 1e-8))
    cov = nearest_psd_cov(cov, eig_floor=eig_floor)
    max_cond = float(rcfg.get("max_cov_condition", 1e12))
    cond = _safe_cond(cov)
    if (not math.isfinite(cond)) or cond > max_cond:
        raise ValueError(f"Precomputed covariance condition number {cond:.3e} exceeds max_cov_condition={max_cond:.3e}.")
    return [CovScenario("Stage2_precomputed", cov)]


# --------------------------
# Black-Litterman
# --------------------------
def black_litterman_posterior(
    pi: np.ndarray,
    Sigma: np.ndarray,
    P: np.ndarray,
    q: np.ndarray,
    Omega: np.ndarray,
    tau: float
) -> np.ndarray:
    """
    Black-Litterman posterior expected returns.
    """
    tauSigma = tau * Sigma
    A = symmetrize(P @ tauSigma @ P.T + Omega)
    b = q - P @ pi
    cond = _safe_cond(A)
    if not math.isfinite(cond) or cond > 1e12:
        logger.warning("BL linear system ill-conditioned (cond=%.2e); using pseudo-inverse.", cond)
        middle = np.linalg.pinv(A)
        mu = pi + tauSigma @ P.T @ middle @ b
    else:
        try:
            x = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            logger.warning("BL linear system singular; using pseudo-inverse.")
            middle = np.linalg.pinv(A)
            mu = pi + tauSigma @ P.T @ middle @ b
        else:
            mu = pi + tauSigma @ P.T @ x
    return mu


def _cfg_path_from_cfg(cfg: Dict[str, Any]) -> Optional[Path]:
    raw = cfg.get("_cfg_path", None)
    if raw is None or str(raw).strip() == "":
        return None
    return Path(str(raw)).expanduser().resolve()


def _normalize_weight_vector(
    raw: pd.Series,
    *,
    n: int,
    label: str,
) -> np.ndarray:
    vals = pd.to_numeric(raw, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0).astype(float)
    vals = vals.clip(lower=0.0)
    total = float(vals.sum())
    if total <= 0.0 or not math.isfinite(total):
        logger.warning("%s produced non-positive benchmark weights; falling back to equal-weight.", label)
        return np.ones(n, dtype=float) / float(n)
    return (vals.to_numpy(dtype=float) / total).astype(float)


def _load_benchmark_weights_from_csv(
    assets: pd.DataFrame,
    csv_path: str,
    cfg: Dict[str, Any],
) -> Optional[pd.Series]:
    cfg_path = _cfg_path_from_cfg(cfg)
    resolved = _resolve_cfg_path(csv_path, cfg, cfg_path)
    try:
        df = pd.read_csv(resolved)
    except Exception as e:
        logger.warning("Failed to read BL benchmark_weights_csv at %s (%s).", resolved, str(e))
        return None

    if df.empty:
        logger.warning("BL benchmark_weights_csv at %s is empty.", resolved)
        return None

    col_map = {str(c).strip().lower(): str(c).strip() for c in df.columns}
    ticker_col = col_map.get("ticker") or col_map.get("symbol")
    weight_col = col_map.get("weight") or col_map.get("benchmark_weight")
    cap_col = col_map.get("marketcap") or col_map.get("market_cap") or col_map.get("cap")
    if ticker_col is None or (weight_col is None and cap_col is None):
        logger.warning(
            "BL benchmark_weights_csv missing required columns. Need Ticker plus Weight or MarketCap."
        )
        return None

    use_col = weight_col if weight_col is not None else cap_col
    sub = df[[ticker_col, use_col]].copy()
    sub[ticker_col] = sub[ticker_col].astype(str).str.upper().str.strip()
    sub[use_col] = pd.to_numeric(sub[use_col], errors="coerce")
    sub = sub.dropna(subset=[ticker_col, use_col]).drop_duplicates(subset=[ticker_col], keep="first")
    if sub.empty:
        logger.warning("BL benchmark_weights_csv at %s had no usable rows after cleaning.", resolved)
        return None

    asset_tickers = assets["Ticker"].astype(str).str.upper().str.strip()
    return asset_tickers.map(sub.set_index(ticker_col)[use_col]).astype(float)


def _build_bl_benchmark_weights(assets: pd.DataFrame, cfg: Dict[str, Any]) -> np.ndarray:
    bl = _require_cfg_section(cfg, "black_litterman")
    n = len(assets)
    if n <= 0:
        raise ValueError("BL benchmark requires at least one asset.")

    configured = bl.get("benchmark_weights", None)
    if configured is not None:
        if isinstance(configured, dict):
            tickers = assets["Ticker"].astype(str).str.upper().str.strip()
            raw = tickers.map(lambda t: configured.get(t, configured.get(str(t).lower(), 0.0)))
            return _normalize_weight_vector(pd.Series(raw, index=assets.index), n=n, label="black_litterman.benchmark_weights")
        if isinstance(configured, (list, tuple, np.ndarray, pd.Series)):
            seq = list(configured)[:n]
            raw = pd.Series(seq, index=assets.index[: len(seq)])
            raw = raw.reindex(assets.index).fillna(0.0)
            return _normalize_weight_vector(raw, n=n, label="black_litterman.benchmark_weights")
        logger.warning(
            "Ignoring unsupported black_litterman.benchmark_weights type %s.",
            type(configured).__name__,
        )

    source = str(bl.get("benchmark_weight_source", "equal")).strip().lower()
    if source in {"equal", "equal_weight", "equal-weight"}:
        return np.ones(n, dtype=float) / float(n)
    if source == "score":
        if "SignalScore" not in assets.columns:
            logger.warning("BL score benchmark requested but assets lack SignalScore; falling back to equal-weight.")
            return np.ones(n, dtype=float) / float(n)
        scores = pd.to_numeric(assets["SignalScore"], errors="coerce")
        ranked = scores.rank(method="average", pct=True)
        return _normalize_weight_vector(ranked, n=n, label="black_litterman.benchmark_weight_source=score")
    if source == "csv":
        csv_path = _opt_str(bl.get("benchmark_weights_csv", bl.get("benchmark_csv", None)))
        if csv_path is None:
            logger.warning("BL benchmark_weight_source=csv requires benchmark_weights_csv; falling back to equal-weight.")
            return np.ones(n, dtype=float) / float(n)
        raw = _load_benchmark_weights_from_csv(assets, csv_path, cfg)
        if raw is None:
            return np.ones(n, dtype=float) / float(n)
        return _normalize_weight_vector(raw, n=n, label="black_litterman.benchmark_weight_source=csv")

    logger.warning("Unknown black_litterman.benchmark_weight_source=%r; falling back to equal-weight.", source)
    return np.ones(n, dtype=float) / float(n)


def build_bl_inputs(
    assets: pd.DataFrame,
    cov_base: np.ndarray,
    rets: pd.DataFrame,
    cfg: Dict[str, Any],
    ppy: int,
    cash_period_return: float
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Build BL posterior expected returns for risky assets only (cash is handled as a separate asset).

    IMPORTANT (Total-return consistency):
      - The classic BL prior pi = delta * Sigma * w_bench is best interpreted as an *excess return*
        (risk premium) vector when a risk-free asset exists.
      - This optimizer includes an explicit CASH asset with expected return `cash_period_return`
        and reports Sharpe vs CASH.
      - Therefore we convert the BL outputs to TOTAL expected returns by adding `cash_period_return`
        when black_litterman.return_space is set to "total".

    Returns:
      mu_bl_risky (n,)  # per-period expected returns (total or excess per return_space)
      pi (n,)           # per-period equilibrium prior returns (total or excess per return_space)
    """
    bl = _require_cfg_section(cfg, "black_litterman")
    tau = float(bl.get("tau", 0.05))
    delta = float(bl.get("delta", 2.5))

    # Benchmark weights:
    # - equal: legacy fallback
    # - score: heuristic score-weighted proxy
    # - csv / benchmark_weights: explicit external override
    n = cov_base.shape[0]
    w_bench = _build_bl_benchmark_weights(assets, cfg)

    # ---- Equilibrium prior (excess) ----
    # Standard reverse-optimization gives an equilibrium *excess return* (risk premium) vector:
    #   pi_excess = delta * Sigma * w_bench
    pi_excess = delta * (cov_base @ w_bench)

    return_space = str(bl.get("return_space", "total")).strip().lower()
    if return_space in {"excess", "risk_premium", "risk-premium"}:
        pi = pi_excess
    else:
        if return_space not in {"total", "total_return", "total-return"}:
            logger.warning("Unknown black_litterman.return_space=%r; defaulting to 'total'.", return_space)
        # Convert to TOTAL expected returns for consistency with explicit CASH in the optimizer:
        #   pi_total = r_f + pi_excess
        rf_p = float(cash_period_return)
        pi = pi_excess + rf_p

    # Build alpha from score signals. The default legacy mode z-scores FinalScore and rescales it.
    # Stage 7 can request absolute annual alpha so calibrated expected-return magnitudes are preserved.
    alpha_mode = str(bl.get("alpha_input_mode", "zscore_scaled")).strip().lower()
    alpha_scale_p = annual_to_period_rate(float(bl.get("alpha_scale_annual", 0.06)), ppy)
    sector_alpha_p = annual_to_period_rate(float(bl.get("sector_alpha_scale_annual", 0.03)), ppy)
    foreign_alpha_p = annual_to_period_rate(float(bl.get("foreign_alpha_scale_annual", 0.03)), ppy)

    # Normalize score signals cross-sectionally.
    # If SignalScoreZ is provided (e.g., computed on full universe), use it; else compute locally.
    if "SignalScoreZ" in assets.columns:
        score_z_s = pd.to_numeric(assets["SignalScoreZ"], errors="coerce")
        if score_z_s.isna().all() or float(score_z_s.std(ddof=0)) <= 1e-12:
            score_z = zscore(assets["SignalScore"])
        else:
            score_z = score_z_s.fillna(0.0)
    else:
        score_z = zscore(assets["SignalScore"])

    if alpha_mode in {"absolute_annual", "annual_alpha", "absolute"}:
        alpha_col = str(bl.get("alpha_column", "ExpectedAlphaAnnual")).strip() or "ExpectedAlphaAnnual"
        if alpha_col not in assets.columns:
            raise ValueError(f"black_litterman.alpha_input_mode={alpha_mode!r} requires assets column {alpha_col!r}.")
        alpha_ann = pd.to_numeric(assets[alpha_col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        if alpha_ann.isna().any():
            bad = assets.loc[alpha_ann.isna(), "Ticker"].astype(str).head(10).tolist()
            raise ValueError(f"{alpha_col} contains non-finite annual alpha values. Sample tickers: {bad}")
        alpha = np.array([annual_to_period_rate(float(v), ppy) for v in alpha_ann.to_numpy(dtype=float)], dtype=float)
    else:
        alpha = alpha_scale_p * score_z.to_numpy(dtype=float)

    # Optional sector signal contribution
    if bool(bl.get("include_sector_in_alpha", True)) and "SectorScoreZ" in assets.columns:
        sector_alpha = sector_alpha_p * assets["SectorScoreZ"].fillna(0.0).to_numpy(dtype=float)
        if bool(bl.get("use_sector_state_alpha_multiplier", False)) and "SectorState" in assets.columns:
            state_cfg = bl.get("sector_state_alpha_multipliers", {}) or {}
            state_mult = assets["SectorState"].map(
                lambda s: float(state_cfg.get(str(s), {"Positive": 1.0, "Neutral": 0.0, "Negative": -0.5}.get(str(s), 0.0)))
            ).fillna(0.0)
            sector_alpha = sector_alpha * state_mult.to_numpy(dtype=float)
        alpha = alpha + sector_alpha

    # Optional foreign signal contribution
    if bool(bl.get("include_foreign_in_alpha", True)) and "ForeignScoreZ" in assets.columns:
        alpha = alpha + foreign_alpha_p * assets["ForeignScoreZ"].fillna(0.0).to_numpy(dtype=float)

    # Views in the same return space as pi:
    #   q = pi + alpha
    q = pi + alpha

    # Confidence -> Omega (diagonal) - vectorized computation
    conf_by_rating = dict(bl.get("confidence_by_rating", {}) or {})
    min_conf = float(bl.get("min_confidence", 0.15))
    max_conf = float(bl.get("max_confidence", 0.95))
    if min_conf <= 0.0:
        logger.warning("min_confidence <= 0; clamping to 1e-4.")
        min_conf = 1e-4
    if max_conf <= min_conf:
        logger.warning("max_confidence <= min_confidence; raising max_confidence.")
        max_conf = min(min_conf + 0.05, 1.0)
    boost = float(bl.get("score_confidence_boost", 0.10))

    # Tier-1: treat FOREIGN as an explicit rating bucket for BL confidence.
    # If not configured, inherit HOLD confidence (or 0.50 if HOLD is also absent).
    fallback_conf = float(conf_by_rating.get("Hold", 0.50))
    if "FOREIGN" not in conf_by_rating:
        conf_by_rating["FOREIGN"] = fallback_conf
        logger.warning(
            "BL confidence_by_rating missing 'FOREIGN'; defaulting to Hold fallback %.3f.",
            fallback_conf,
        )

    ratings = assets["Rating"].astype(str).tolist()
    unknown_ratings = sorted({r for r in ratings if r not in conf_by_rating})
    if unknown_ratings:
        logger.warning(
            "BL confidence_by_rating missing labels %s; using fallback confidence %.3f for those assets.",
            unknown_ratings,
            fallback_conf,
        )
    c0 = np.array([float(conf_by_rating.get(r, fallback_conf)) for r in ratings], dtype=float)
    if bool(bl.get("use_score_confidence_in_omega", False)) and "ScoreConfidence" in assets.columns:
        score_conf = pd.to_numeric(assets["ScoreConfidence"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        score_conf = score_conf.fillna(0.0).clip(lower=0.0, upper=1.0).to_numpy(dtype=float)
        c0 = c0 * score_conf
    conf = c0 + boost * np.abs(score_z.to_numpy(dtype=float))
    conf = np.clip(conf, min_conf, max_conf)

    # Omega_i proportional to asset variance and inverse confidence
    # Higher confidence => smaller Omega
    var = np.diag(cov_base)
    inv_conf = 1.0 / np.clip(conf, 1e-6, 1.0)
    Omega_diag = (tau * var) * (inv_conf - 1.0)
    Omega_diag = np.maximum(Omega_diag, 1e-12)
    Omega = np.diag(Omega_diag)

    # P = Identity (absolute views on each asset)
    P = np.eye(n)

    mu_bl = black_litterman_posterior(pi=pi, Sigma=cov_base, P=P, q=q, Omega=Omega, tau=tau)
    return mu_bl, pi


# --------------------------
# Diversification clustering
# --------------------------
def cluster_assets(corr: np.ndarray, tickers: List[str], max_clusters: int, linkage_method: str) -> Dict[str, int]:
    """
    Hierarchical clustering on correlation distance.
    """
    corr = np.clip(corr, -1.0, 1.0)
    dist = np.sqrt((1.0 - corr) / 2.0)
    np.fill_diagonal(dist, 0.0)
    condensed = squareform(dist, checks=False)
    Z = linkage(condensed, method=linkage_method)
    labels = fcluster(Z, t=max_clusters, criterion="maxclust")
    return {t: int(labels[i]) for i, t in enumerate(tickers)}


# --------------------------
# Optimization
# --------------------------
@dataclass
class OptResult:
    portfolio_name: str
    weights: pd.DataFrame  # columns: Ticker, Weight, Low, High, Sleeve, Sector, etc.
    metrics: Dict[str, Any]


def _get_region_budgets_and_mode(
    cfg: Dict[str, Any],
    *,
    is_long_short: bool,
) -> Tuple[Dict[str, Any], str]:
    """
    Tier-1:
      - long_only: region_budgets apply to NET weights (all non-negative, so net==long).
      - long_short: allow region budgets to apply in one of three exposure spaces:
          * net   : sum(w)       (long - short)
          * long  : sum(w_plus)  (long book only)
          * gross : sum(w_plus + w_minus)

    Config:
      allocation:
        region_budgets: {...}              # long-only defaults
        long_short:
          region_budget_mode: "long"       # "net" | "long" | "gross"
          region_budgets: {...}            # optional override for long/short
    """
    acfg = cfg.get("allocation", {}) or {}
    budgets = (acfg.get("region_budgets", {}) or {})
    mode = "net"

    if is_long_short:
        lscfg = acfg.get("long_short", {}) or {}
        mode = str(lscfg.get("region_budget_mode", "net")).strip().lower()
        budgets = (lscfg.get("region_budgets", budgets) or budgets) or {}

    if mode not in {"net", "long", "gross"}:
        logger.warning("Unknown region_budget_mode=%r; falling back to 'net'.", mode)
        mode = "net"

    return budgets, mode


def _get_prune_reoptimize_cfg(cfg: Dict[str, Any], *, is_long_short: bool) -> Dict[str, Any]:
    """
    Option-1 (optimize -> prune -> reoptimize) config:
      optimization:
        prune_reoptimize:
          enabled: true|false
          max_passes: 2
          min_weight: 0.0025                # fallback (25 bps)
          min_weight_long_only: 0.0025
          min_abs_weight_long_short: 0.0025
          min_total_names: 6                # don't prune below this total risky count
    """
    ocfg = _require_cfg_section(cfg, "optimization")
    pcfg = ocfg.get("prune_reoptimize", {}) or {}
    enabled = bool(pcfg.get("enabled", False))
    max_passes = int(pcfg.get("max_passes", 2))
    min_total_names = int(pcfg.get("min_total_names", 6))

    base_min = float(pcfg.get("min_weight", 0.0025))
    if is_long_short:
        min_w = float(pcfg.get("min_abs_weight_long_short", base_min))
        use_abs = True
    else:
        min_w = float(pcfg.get("min_weight_long_only", base_min))
        use_abs = False  # long-only is non-negative anyway

    # normalize/sanitize
    max_passes = max(1, max_passes)
    min_total_names = max(1, min_total_names)
    min_w = max(0.0, float(min_w))

    return {
        "enabled": enabled,
        "max_passes": max_passes,
        "min_weight": min_w,
        "use_abs": use_abs,
        "min_total_names": min_total_names,
    }


def _get_infeasible_handling_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tier-1: Policy-driven feasibility restoration. Never silently return a partial pass.

    optimization:
      infeasible_handling:
        enabled: true
        action: "auto_relax_then_fail" | "fail"
        add_back_names:
          enabled: true
          step: 10
          max_total_names_long_only: 80
          max_total_names_long_short: 120
          order_by: "prev_weight" | "signal_score"
        relax_cash_max:
          enabled: true
          max_extra_cash: 0.20
          penalty: 5000.0
    """
    ocfg = _require_cfg_section(cfg, "optimization")
    icfg = ocfg.get("infeasible_handling", {}) or {}

    enabled = bool(icfg.get("enabled", True))
    action = str(icfg.get("action", "auto_relax_then_fail")).strip().lower()
    if action not in {"auto_relax_then_fail", "fail"}:
        logger.warning(
            "Unknown optimization.infeasible_handling.action=%r; defaulting to auto_relax_then_fail.",
            action,
        )
        action = "auto_relax_then_fail"

    add_cfg = icfg.get("add_back_names", {}) or {}
    cash_cfg = icfg.get("relax_cash_max", {}) or {}

    return {
        "enabled": enabled,
        "action": action,
        "add_back": {
            "enabled": bool(add_cfg.get("enabled", True)),
            "step": int(add_cfg.get("step", 10)),
            "max_total_names_long_only": add_cfg.get("max_total_names_long_only", None),
            "max_total_names_long_short": add_cfg.get("max_total_names_long_short", None),
            "order_by": str(add_cfg.get("order_by", "prev_weight")).strip().lower(),
        },
        "relax_cash": {
            "enabled": bool(cash_cfg.get("enabled", False)),
            "max_extra_cash": float(cash_cfg.get("max_extra_cash", 0.0)),
            "penalty": float(cash_cfg.get("penalty", 0.0)),
        },
    }


def _is_infeasible_runtime_error(e: Exception) -> bool:
    if isinstance(e, OptimizationInfeasibleError):
        return True
    msg = str(e).strip().lower()
    return (
        msg.startswith("optimization infeasible for ")
        or msg.endswith("status: infeasible")
        or msg.endswith("status: infeasible_inaccurate")
    )


def _augment_assets_rets_keep_order(
    *,
    assets_keep: pd.DataFrame,
    rets_keep: pd.DataFrame,
    assets_pool: pd.DataFrame,
    rets_pool: pd.DataFrame,
    add_tickers: List[str],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Add tickers back from (assets_pool, rets_pool) while preserving the current asset order.
    Ensures rets columns align to assets order.
    """
    keep_order = assets_keep["Ticker"].astype(str).str.upper().tolist()
    add_order = [str(t).upper().strip() for t in add_tickers]
    order = list(dict.fromkeys(keep_order + add_order))

    pool = assets_pool.copy()
    pool["Ticker"] = pool["Ticker"].astype(str).str.upper().str.strip()
    pool = pool.drop_duplicates(subset=["Ticker"], keep="first").set_index("Ticker", drop=False)

    present = [t for t in order if t in pool.index]
    present_with_rets = [t for t in present if t in rets_pool.columns]
    assets_aug = pool.loc[present_with_rets].copy().reset_index(drop=True)

    rets_aug = rets_pool.loc[:, present_with_rets].copy()
    tickers_aug = assets_aug["Ticker"].astype(str).str.upper().tolist()
    rets_aug = rets_aug.loc[:, [t for t in tickers_aug if t in rets_aug.columns]]
    if tickers_aug != list(rets_aug.columns):
        raise AssertionError("assets/rets ticker alignment mismatch after add-back augmentation")
    return assets_aug, rets_aug


def _required_names_for_region_mins(
    assets: pd.DataFrame,
    cfg: Dict[str, Any],
    *,
    is_long_short: bool,
) -> Tuple[int, int]:
    """
    Guardrail so pruning doesn't make region min budgets infeasible given per-asset max weights.
    Returns (req_us_names, req_foreign_names).
    """
    ocfg = _require_cfg_section(cfg, "optimization")
    budgets, budget_mode = _get_region_budgets_and_mode(cfg, is_long_short=is_long_short)

    min_us = float((budgets.get("US", {}) or {}).get("min", 0.0))
    min_fx = float((budgets.get("FOREIGN", {}) or {}).get("min", 0.0))

    if is_long_short:
        ls_cfg = ocfg.get("long_short", {}) or {}
        max_long = float(ls_cfg.get("max_long_per_stock", 0.06))
        max_short = float(ls_cfg.get("max_short_per_stock", -0.05))
        # If budgets are on gross exposure, a per-name gross cap can be as large as max(max_long, abs(max_short)).
        # If budgets are net or long exposure, max_long is the right conservative cap.
        ub_us = max(max_long, abs(max_short)) if budget_mode == "gross" else max_long
        ub_fx = float(ls_cfg.get("max_weight_per_foreign_etf", 0.20))
    else:
        lo_cfg = ocfg.get("long_only", {}) or {}
        ub_us = float(lo_cfg.get("max_weight_per_stock", 0.08))
        ub_fx = float(lo_cfg.get("max_weight_per_foreign_etf", 0.20))

    ub_us = max(1e-12, ub_us)
    ub_fx = max(1e-12, ub_fx)

    req_us = int(math.ceil(min_us / ub_us)) if min_us > 0 else 0
    req_fx = int(math.ceil(min_fx / ub_fx)) if min_fx > 0 else 0

    # Can't require more than exists in the current universe
    n_us = int((assets["Sleeve"] == "US").sum())
    n_fx = int((assets["Sleeve"] == "FOREIGN").sum())
    req_us = min(req_us, n_us)
    req_fx = min(req_fx, n_fx)

    return req_us, req_fx


def _compute_dispersion_scaling(
    *,
    rets: Optional[pd.DataFrame],
    cfg: Dict[str, Any],
    base_risk_aversion: float,
) -> Dict[str, Any]:
    ocfg = _require_cfg_section(cfg, "optimization")
    dscfg = (ocfg.get("dispersion_scaling", {}) or {})
    enabled = bool(dscfg.get("enabled", False))
    diagnostics: Dict[str, Any] = {
        "dispersion_scaling_enabled": bool(enabled),
        "dispersion_scaling_multiplier": 1.0,
        "dispersion_scaling_current": float("nan"),
        "dispersion_scaling_baseline": float("nan"),
        "dispersion_scaling_ratio": float("nan"),
        "risk_aversion_base": float(base_risk_aversion),
        "risk_aversion_effective": float(base_risk_aversion),
    }
    if (not enabled) or rets is None or rets.empty:
        return diagnostics

    lookback_days = max(5, int(dscfg.get("lookback_days", 20)))
    baseline_days = max(lookback_days, int(dscfg.get("baseline_days", 126)))
    min_assets = max(3, int(dscfg.get("min_assets", 10)))
    min_multiplier = float(np.clip(dscfg.get("min_multiplier", 0.85), 0.10, 10.0))
    max_multiplier = float(np.clip(dscfg.get("max_multiplier", 1.15), min_multiplier, 10.0))
    sensitivity = max(0.0, float(dscfg.get("sensitivity", 0.75)))

    ret_num = rets.apply(pd.to_numeric, errors="coerce")
    counts = ret_num.count(axis=1)
    xs_std = ret_num.std(axis=1, ddof=1).where(counts >= min_assets).dropna()
    if len(xs_std) < lookback_days:
        diagnostics["dispersion_scaling_enabled"] = False
        return diagnostics

    current = float(xs_std.tail(lookback_days).mean())
    baseline = float(xs_std.tail(baseline_days).median())
    if (not np.isfinite(current)) or (not np.isfinite(baseline)) or baseline <= 1e-12:
        diagnostics["dispersion_scaling_enabled"] = False
        return diagnostics

    ratio = float(current / baseline)
    multiplier = float(np.clip(1.0 + sensitivity * (ratio - 1.0), min_multiplier, max_multiplier))
    effective_risk_aversion = float(base_risk_aversion / multiplier) if multiplier > 1e-12 else float(base_risk_aversion)
    diagnostics.update(
        {
            "dispersion_scaling_enabled": True,
            "dispersion_scaling_multiplier": multiplier,
            "dispersion_scaling_current": current,
            "dispersion_scaling_baseline": baseline,
            "dispersion_scaling_ratio": ratio,
            "risk_aversion_effective": effective_risk_aversion,
        }
    )
    return diagnostics


def _prune_assets_by_weight(
    assets: pd.DataFrame,
    rets: pd.DataFrame,
    w: pd.Series,
    cfg: Dict[str, Any],
    *,
    is_long_short: bool,
    min_weight: float,
    use_abs: bool,
    min_total_names: int,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    """
    Returns (assets_pruned, rets_pruned, dropped_tickers).
    Drops risky assets whose (abs) weight is below min_weight, but:
      - never prunes below min_total_names risky assets
      - never prunes below required counts to satisfy US/FOREIGN min budgets (approx guardrail)
    """
    ucfg = _require_cfg_section(cfg, "universe")
    cash_symbol = str(ucfg.get("cash_symbol", "CASH")).upper()

    tickers = assets["Ticker"].astype(str).str.upper().tolist()
    w_risky = w.drop(labels=[cash_symbol], errors="ignore").copy()

    # Align w_risky to the current assets list (ignore any extraneous index entries)
    w_risky = w_risky.reindex(tickers).fillna(0.0)
    metric = w_risky.abs() if use_abs else w_risky

    # Candidates are strictly below threshold
    cand = [t for t in tickers if float(metric.get(t, 0.0)) < float(min_weight)]
    if not cand:
        return assets, rets, []

    cand_set = set(cand)
    # Start by keeping everyone not in cand
    keep = [t for t in tickers if t not in cand_set]

    # Guardrails: keep enough names per sleeve for region mins
    req_us, req_fx = _required_names_for_region_mins(assets, cfg, is_long_short=is_long_short)
    sleeve_by_ticker = assets.set_index("Ticker")["Sleeve"].to_dict()

    def _ensure_min_count(sleeve: str, req: int) -> None:
        if req <= 0:
            return
        keep_now = [t for t in keep if sleeve_by_ticker.get(t) == sleeve]
        if len(keep_now) >= req:
            return
        need = req - len(keep_now)
        # Add back from candidates in that sleeve, highest metric first (closest to threshold)
        pool = [t for t in cand if sleeve_by_ticker.get(t) == sleeve and t not in set(keep)]
        pool = sorted(pool, key=lambda t: float(metric.get(t, 0.0)), reverse=True)
        keep.extend(pool[:need])

    _ensure_min_count("US", req_us)
    _ensure_min_count("FOREIGN", req_fx)

    # Guardrail: keep at least min_total_names risky assets overall
    if len(keep) < min_total_names:
        need = min_total_names - len(keep)
        pool = [t for t in cand if t not in set(keep)]
        pool = sorted(pool, key=lambda t: float(metric.get(t, 0.0)), reverse=True)
        keep.extend(pool[:need])

    keep = list(dict.fromkeys(keep))  # de-dupe, preserve order
    keep_set = set(keep)
    dropped = [t for t in tickers if t not in keep_set]
    if not dropped:
        return assets, rets, []

    # Subset assets/rets
    assets2 = assets[assets["Ticker"].isin(keep)].copy().reset_index(drop=True)
    cols = [t for t in keep if t in rets.columns]
    rets2 = rets.loc[:, cols].copy()

    # Ensure rets columns order matches assets order (important for covariance/mu alignment)
    assets_order = assets2["Ticker"].astype(str).str.upper().tolist()
    rets2 = rets2.loc[:, [t for t in assets_order if t in rets2.columns]]

    return assets2, rets2, dropped


def optimize_prune_reoptimize(
    *,
    portfolio_name: str,
    assets: pd.DataFrame,
    rets: pd.DataFrame,
    cfg: Dict[str, Any],
    ppy: int,
    cash_p: float,
    rng: np.random.Generator,
    prev_weights: Optional[pd.Series],
    is_long_short: bool,
    initial_turnover_extra: float = 0.0,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[CovScenario], np.ndarray, pd.Series, Dict[str, Any], float]:
    """
    Option-1: optimize -> prune -> reoptimize (iterative up to max_passes).

    Returns:
      (final_assets, final_rets, final_cov_scenarios, final_mu_risky, final_weights, final_metrics, turnover_extra)

    turnover_extra:
      Constant L1 turnover from weights of tickers that no longer exist in the decision vector,
      including any initial pre-optimization drops plus names pruned during re-optimization.
      Used to keep max_turnover constraints honest (Tier-1 requirement).
    """
    pcfg = _get_prune_reoptimize_cfg(cfg, is_long_short=is_long_short)
    ih = _get_infeasible_handling_cfg(cfg)
    cash_relax_enabled = bool((ih.get("relax_cash", {}) or {}).get("enabled", False))
    enabled = bool(pcfg.get("enabled", False))
    max_passes = int(pcfg.get("max_passes", 1)) if enabled else 1
    min_w = float(pcfg.get("min_weight", 0.0))
    use_abs = bool(pcfg.get("use_abs", bool(is_long_short)))
    min_total_names = int(pcfg.get("min_total_names", 1))

    turnover_extra = float(initial_turnover_extra)
    initial_turnover_extra = float(initial_turnover_extra)
    dropped_total: List[str] = []

    assets_cur = assets.copy()
    rets_cur = rets.copy()

    # Stash the full pre-prune universe so feasibility restoration can recover all prior drops.
    stash_assets: Optional[pd.DataFrame] = assets_cur.copy()
    stash_rets: Optional[pd.DataFrame] = rets_cur.copy()
    stash_dropped: List[str] = []
    stash_w: Optional[pd.Series] = None

    last_cov: List[CovScenario] = []
    last_mu: np.ndarray = np.zeros(len(assets_cur), dtype=float)
    last_w: pd.Series = pd.Series(dtype=float)
    last_metrics: Dict[str, Any] = {}

    for k in range(max_passes):
        # Skip bootstrap during pruning passes for performance; only need Pearson+Kendall for base risk
        is_final_pass = (not enabled) or (k >= max_passes - 1)
        cov_scen = build_cov_scenarios(rets_cur, cfg, rng, include_bootstrap=is_final_pass)
        cov_base = (
            np.mean([cov_scen[0].cov, cov_scen[1].cov], axis=0)
            if len(cov_scen) >= 2
            else cov_scen[0].cov
        )
        mu_risky, _ = build_bl_inputs(assets_cur, cov_base, rets_cur, cfg, ppy, cash_p)

        pass_name = portfolio_name if k == 0 else f"{portfolio_name}_PASS{k+1}"

        # Ensure these are always bound for static analysis (Pylance reportPossiblyUnboundVariable).
        w_opt: pd.Series = pd.Series(dtype=float)
        metrics: Dict[str, Any] = {}
        try:
            w_opt, metrics = solve_portfolio(
                portfolio_name=pass_name,
                assets=assets_cur,
                rets=rets_cur,
                mu_risky=mu_risky,
                cov_scenarios=cov_scen[:2],  # base risk uses Pearson+Kendall; bootstraps reserved for bands
                cfg=cfg,
                ppy=ppy,
                cash_period_return=cash_p,
                prev_weights=prev_weights,
                is_long_short=is_long_short,
                turnover_extra=turnover_extra,
                allow_cash_budget_relaxation=cash_relax_enabled,
            )
        except RuntimeError as e:
            # Tier-1: never return "half-optimized". Either repair and re-optimize, or fail.
            if (not ih["enabled"]) or (ih["action"] == "fail") or (not _is_infeasible_runtime_error(e)):
                raise
            if stash_assets is None or stash_rets is None:
                raise

            logger.warning("%s infeasible. Attempting feasibility restoration by adding back pruned names.", pass_name)

            add_cfg = ih["add_back"]
            assets_try = assets_cur.copy()
            rets_try = rets_cur.copy()
            added: List[str] = []
            solved = False
            candidates: List[str] = []
            if bool(add_cfg.get("enabled", True)) and stash_dropped:
                step = max(1, int(add_cfg.get("step", 10)))
                max_total = (
                    add_cfg.get("max_total_names_long_short")
                    if is_long_short
                    else add_cfg.get("max_total_names_long_only")
                )
                if max_total is None:
                    max_total = len(stash_assets)
                max_total = int(min(max_total, len(stash_assets)))

                # Candidates = dropped names not currently in the pruned universe
                cur_set = set(assets_cur["Ticker"].astype(str).str.upper().tolist())
                candidates = [t for t in stash_dropped if str(t).upper() not in cur_set]
                candidates = list(dict.fromkeys([str(t).upper() for t in candidates]))

                # Order candidates
                order_by = str(add_cfg.get("order_by", "prev_weight")).strip().lower()
                if order_by == "signal_score":
                    score_map = stash_assets.set_index("Ticker")["SignalScore"].to_dict()
                    candidates = sorted(candidates, key=lambda t: float(score_map.get(t, -np.inf)), reverse=True)
                else:
                    # default: order by abs(prev weight) so you add back the "closest to threshold" names first
                    if stash_w is not None:
                        stash_w_s = cast(pd.Series, stash_w)

                        def _prev_weight_key(t: str) -> float:
                            val = stash_w_s.get(t, 0.0)
                            if isinstance(val, pd.Series):
                                arr = pd.to_numeric(val, errors="coerce").fillna(0.0).to_numpy()
                                return float(np.sum(np.abs(arr)))
                            return float(abs(val))

                        candidates = sorted(candidates, key=_prev_weight_key, reverse=True)

                while (len(assets_try) < max_total) and (len(added) < len(candidates)):
                    take = min(step, max_total - len(assets_try), len(candidates) - len(added))
                    batch = candidates[len(added): len(added) + take]
                    added.extend(batch)

                    assets_try, rets_try = _augment_assets_rets_keep_order(
                        assets_keep=assets_try,
                        rets_keep=rets_try,
                        assets_pool=stash_assets,
                        rets_pool=stash_rets,
                        add_tickers=batch,
                    )
                    assets_try_order = assets_try["Ticker"].astype(str).str.upper().tolist()
                    rets_try_order = [str(c).upper() for c in rets_try.columns.tolist()]
                    if assets_try_order != rets_try_order:
                        raise RuntimeError(
                            "Internal error: assets/rets order mismatch after add-back recovery "
                            f"(assets={len(assets_try_order)}, rets={len(rets_try_order)})."
                        )

                    cov_scen_try = build_cov_scenarios(rets_try, cfg, rng, include_bootstrap=is_final_pass)
                    cov_base_try = (
                        np.mean([cov_scen_try[0].cov, cov_scen_try[1].cov], axis=0)
                        if len(cov_scen_try) >= 2
                        else cov_scen_try[0].cov
                    )
                    mu_try, _ = build_bl_inputs(assets_try, cov_base_try, rets_try, cfg, ppy, cash_p)

                    try:
                        w_opt, metrics = solve_portfolio(
                            portfolio_name=f"{pass_name}__ADD_BACK_{len(added)}",
                            assets=assets_try,
                            rets=rets_try,
                            mu_risky=mu_try,
                            cov_scenarios=cov_scen_try[:2],
                            cfg=cfg,
                            ppy=ppy,
                            cash_period_return=cash_p,
                            prev_weights=prev_weights,
                            is_long_short=is_long_short,
                            turnover_extra=turnover_extra,
                            allow_cash_budget_relaxation=cash_relax_enabled,
                        )
                        # Success: adopt repaired universe & diagnostics
                        cov_scen = cov_scen_try
                        mu_risky = mu_try
                        assets_cur = assets_try
                        rets_cur = rets_try
                        metrics = dict(metrics)
                        metrics.update({
                            "feasibility_repair_used": True,
                            "feasibility_repair_method": "add_back_names",
                            "feasibility_repair_added_names": int(len(added)),
                        })
                        if prev_weights is not None and added:
                            for t in added:
                                turnover_extra = max(0.0, turnover_extra - float(abs(prev_weights.get(t, 0.0))))
                        if added:
                            added_set = set(added)
                            dropped_total = [t for t in dropped_total if t not in added_set]
                        solved = True
                        break
                    except RuntimeError as e2:
                        if not _is_infeasible_runtime_error(e2):
                            raise

            if not solved:
                # Last resort (optional): allow cash max relaxation (elastic). Still fully optimized.
                if ih["relax_cash"]["enabled"]:
                    cov_scen_try = build_cov_scenarios(rets_try, cfg, rng, include_bootstrap=is_final_pass)
                    cov_base_try = (
                        np.mean([cov_scen_try[0].cov, cov_scen_try[1].cov], axis=0)
                        if len(cov_scen_try) >= 2
                        else cov_scen_try[0].cov
                    )
                    mu_try, _ = build_bl_inputs(assets_try, cov_base_try, rets_try, cfg, ppy, cash_p)
                    w_opt, metrics = solve_portfolio(
                        portfolio_name=f"{pass_name}__CASH_RELAX",
                        assets=assets_try,
                        rets=rets_try,
                        mu_risky=mu_try,
                        cov_scenarios=cov_scen_try[:2],
                        cfg=cfg,
                        ppy=ppy,
                        cash_period_return=cash_p,
                        prev_weights=prev_weights,
                        is_long_short=is_long_short,
                        turnover_extra=turnover_extra,
                        allow_cash_budget_relaxation=True,
                    )
                    cov_scen = cov_scen_try
                    mu_risky = mu_try
                    assets_cur = assets_try
                    rets_cur = rets_try
                    metrics = dict(metrics)
                    metrics.update({
                        "feasibility_repair_used": True,
                        "feasibility_repair_method": "cash_max_relaxation",
                        "feasibility_repair_added_names": int(len(added)),
                    })
                else:
                    raise

        # Defensive guard: if we got here, we must have a real solution.
        if w_opt.empty:
            raise RuntimeError(f"{pass_name}: internal error (no weights returned).")

        last_cov = cov_scen
        last_mu = mu_risky
        last_w = w_opt
        last_metrics = metrics

        # No pruning after last pass (or if disabled)
        if is_final_pass:
            break

        # Keep full stash immutable; update latest solution weights for add-back ordering.
        stash_w = w_opt.copy()

        assets_next, rets_next, dropped = _prune_assets_by_weight(
            assets_cur,
            rets_cur,
            w_opt,
            cfg,
            is_long_short=is_long_short,
            min_weight=min_w,
            use_abs=use_abs,
            min_total_names=min_total_names,
        )

        if not dropped:
            break

        # Update turnover_extra for dropped names (so max_turnover remains valid)
        if prev_weights is not None and len(dropped) > 0:
            for t in dropped:
                turnover_extra += float(abs(prev_weights.get(t, 0.0)))

        dropped_total.extend(dropped)
        stash_dropped = list(dict.fromkeys(stash_dropped + [str(t).upper() for t in dropped]))
        assets_cur = assets_next
        rets_cur = rets_next

    # Attach pruning diagnostics to metrics
    last_metrics = dict(last_metrics)
    last_metrics.update({
        "prune_enabled": bool(enabled),
        "prune_max_passes": int(max_passes),
        "prune_min_weight": float(min_w),
        "prune_use_abs": bool(use_abs),
        "prune_dropped_count": int(len(set(dropped_total))),
        "turnover_extra_initial": float(initial_turnover_extra),
        "turnover_extra_from_pruned": float(max(0.0, turnover_extra - initial_turnover_extra)),
        "turnover_extra_total": float(turnover_extra),
    })

    return assets_cur, rets_cur, last_cov, last_mu, last_w, last_metrics, turnover_extra


def solve_portfolio(
    portfolio_name: str,
    assets: pd.DataFrame,
    rets: Optional[pd.DataFrame],
    mu_risky: np.ndarray,
    cov_scenarios: List[CovScenario],
    cfg: Dict[str, Any],
    ppy: int,
    cash_period_return: float,
    prev_weights: Optional[pd.Series] = None,
    is_long_short: bool = False,
    turnover_extra: float = 0.0,
    allow_cash_budget_relaxation: bool = False,
) -> Tuple[pd.Series, Dict[str, Any]]:
    """
    Solve a single optimization using cfg['risk']['robust_mode'] for the base solution.
    """
    if cp is None:
        raise RuntimeError("cvxpy is required for optimization. Install it: pip install cvxpy")

    ocfg = _require_cfg_section(cfg, "optimization")
    rcfg = _require_cfg_section(cfg, "risk")
    dcfg = cfg.get("diversification", {}) or {}
    acfg = cfg.get("allocation", {}) or {}
    ucfg = _require_cfg_section(cfg, "universe")
    lo_cfg = ocfg.get("long_only", {}) or {}
    ls_cfg = ocfg.get("long_short", {}) or {}

    budgets, region_budget_mode = _get_region_budgets_and_mode(cfg, is_long_short=is_long_short)

    ih_cfg = ocfg.get("infeasible_handling", {}) or {}
    cash_relax_cfg = (ih_cfg.get("relax_cash_max", {}) or {})
    cash_relax_enabled = bool(allow_cash_budget_relaxation) and bool(cash_relax_cfg.get("enabled", False))
    cash_relax_penalty = float(cash_relax_cfg.get("penalty", 0.0)) if cash_relax_enabled else 0.0
    cash_relax_max_extra = float(cash_relax_cfg.get("max_extra_cash", 0.0)) if cash_relax_enabled else 0.0
    if cash_relax_max_extra <= 0.0:
        cash_relax_enabled = False
    cash_max_slack = None  # cp.Variable, created only if CASH budget exists and relaxation enabled

    solver = str(ocfg.get("solver", "ECOS"))
    robust_mode = str(rcfg.get("robust_mode", "average")).lower()

    base_risk_aversion = float(ocfg.get("risk_aversion", 8.0))
    dispersion_diag = _compute_dispersion_scaling(
        rets=rets,
        cfg=cfg,
        base_risk_aversion=base_risk_aversion,
    )
    risk_aversion = float(dispersion_diag["risk_aversion_effective"])
    hhi_penalty = float(ocfg.get("hhi_penalty", 0.20))
    turnover_penalty = float(ocfg.get("turnover_penalty", 0.10))
    max_turnover = ocfg.get("max_turnover", None)

    tickers_risky = assets["Ticker"].tolist()
    n_risky = len(tickers_risky)
    if n_risky < 1:
        raise ValueError(f"{portfolio_name}: no risky assets available for optimization.")
    if int(mu_risky.shape[0]) != int(n_risky):
        raise ValueError(
            f"{portfolio_name}: mu_risky length {int(mu_risky.shape[0])} "
            f"does not match assets length {n_risky}."
        )

    # Full decision includes cash as last element
    n = n_risky + 1
    w = cp.Variable(n)

    # Tier-1 split variables for long/short exposure accounting (risky assets only)
    w_plus = None   # cp.Variable(n_risky, nonneg=True)
    w_minus = None  # cp.Variable(n_risky, nonneg=True)

    # Build full mu (period)
    mu_full = np.zeros(n, dtype=float)
    mu_full[:n_risky] = mu_risky
    mu_full[-1] = cash_period_return

    # -----------------------
    # Minimum expected return (Target-return portfolio constraint)
    # Tier-1: support "hard" (strict) or "soft" (slack + penalty) modes.
    # Also allow per-strategy overrides under optimization.long_only / optimization.long_short.
    # -----------------------
    strat_cfg = ocfg.get("long_short" if is_long_short else "long_only", {}) or {}
    min_ret_ann_raw = strat_cfg.get("min_expected_return_annual", ocfg.get("min_expected_return_annual", None))
    min_ret_mode = str(strat_cfg.get("min_expected_return_mode", ocfg.get("min_expected_return_mode", "none"))).strip().lower()
    min_ret_is_excess = bool(strat_cfg.get("min_return_is_excess_over_cash", ocfg.get("min_return_is_excess_over_cash", False)))
    min_ret_penalty = float(strat_cfg.get("min_return_shortfall_penalty", ocfg.get("min_return_shortfall_penalty", 1000.0)))
    min_ret_target_p: Optional[float] = None
    # Keep the parsed annual target as a concrete float for metrics (avoids Optional/None typing issues).
    min_ret_target_ann: float = float("nan")
    min_ret_slack = None  # cp.Variable in soft mode
    min_ret_expr = None   # cp.Expression
    min_return_constraints: List[Any] = []

    # Group indices
    idx_us = [i for i, s in enumerate(assets["Sleeve"].tolist()) if s == "US"]
    idx_foreign = [i for i, s in enumerate(assets["Sleeve"].tolist()) if s == "FOREIGN"]
    idx_cash = [n - 1]
    idx_risky = list(range(n_risky))

    # Bounds per asset
    lb = np.zeros(n, dtype=float)
    ub = np.ones(n, dtype=float)

    if not is_long_short:
        ub_stock = float(lo_cfg.get("max_weight_per_stock", 0.08))
        ub_etf = float(lo_cfg.get("max_weight_per_foreign_etf", 0.20))
        for i in idx_us:
            ub[i] = ub_stock
            lb[i] = 0.0
        for i in idx_foreign:
            ub[i] = ub_etf
            lb[i] = 0.0
        # cash bounds will be handled by region budgets
        lb[-1] = 0.0
        ub[-1] = 1.0
    else:
        max_long = float(ls_cfg.get("max_long_per_stock", 0.06))
        max_short = float(ls_cfg.get("max_short_per_stock", -0.05))
        allow_short_etfs = bool(ls_cfg.get("allow_short_foreign_etfs", False))

        if "LS_Book" in assets.columns:
            book = assets["LS_Book"].astype(str).str.upper().tolist()
        else:
            book = [
                ("SHORT" if str(r).strip() in {"Sell", "Strong Sell"} else "LONG")
                for r in assets["Rating"].astype(str).tolist()
            ]

        for i in idx_us:
            b = str(book[i]).upper()
            if b == "SHORT":
                ub[i] = 0.0
                lb[i] = max_short  # negative
            else:
                ub[i] = max_long
                lb[i] = 0.0
        for i in idx_foreign:
            ub[i] = float(
                ls_cfg.get(
                    "max_weight_per_foreign_etf",
                    0.20,
                )
            )
            lb[i] = -ub[i] if allow_short_etfs else 0.0

        # cash can be >=0 only by default
        lb[-1] = 0.0
        ub[-1] = 1.0

    constraints = []
    constraints.append(cp.sum(w) == 1.0)
    constraints.append(w >= lb)
    constraints.append(w <= ub)

    # -------- Tier-1: create w_plus/w_minus for long/short --------
    # This enables:
    #   - region budgets in "long" or "gross" space (linear, DCP-safe)
    #   - clean gross exposure constraints without norm1
    #   - cleaner borrow-cost modeling on shorts
    if is_long_short:
        w_plus = cp.Variable(n_risky, nonneg=True, name="w_plus")
        w_minus = cp.Variable(n_risky, nonneg=True, name="w_minus")

        # Link net weights to split variables
        constraints.append(w[idx_risky] == w_plus - w_minus)

        # Enforce per-asset bounds in split space.
        # If ub_i == 0 => w_plus_i == 0 (short-only assets)
        # If lb_i == 0 => w_minus_i == 0 (long-only assets)
        ub_pos = np.maximum(ub[:n_risky], 0.0)
        ub_neg = np.maximum(-lb[:n_risky], 0.0)
        constraints.append(w_plus <= ub_pos)
        constraints.append(w_minus <= ub_neg)

        def _opt_float(val: Any) -> Optional[float]:
            if val is None:
                return None
            if isinstance(val, str) and val.strip().lower() in {"", "none", "null"}:
                return None
            return float(val)

        # Optional: require a short book / target leverage
        min_short_gross = _opt_float(ls_cfg.get("min_short_gross", None))
        max_short_gross = _opt_float(ls_cfg.get("max_short_gross", None))
        min_long_gross = _opt_float(ls_cfg.get("min_long_gross", None))
        max_long_gross = _opt_float(ls_cfg.get("max_long_gross", None))

        if min_short_gross is not None and max_short_gross is not None and min_short_gross > max_short_gross:
            raise ValueError("min_short_gross cannot be greater than max_short_gross.")
        if min_long_gross is not None and max_long_gross is not None and min_long_gross > max_long_gross:
            raise ValueError("min_long_gross cannot be greater than max_long_gross.")

        short_gross = cp.sum(w_minus)
        long_gross = cp.sum(w_plus)

        if min_short_gross is not None:
            constraints.append(short_gross >= float(min_short_gross))
        if max_short_gross is not None:
            constraints.append(short_gross <= float(max_short_gross))
        if min_long_gross is not None:
            constraints.append(long_gross >= float(min_long_gross))
        if max_long_gross is not None:
            constraints.append(long_gross <= float(max_long_gross))

    # Minimum expected return constraint (optional)
    # Note: expression is in "period" units to match mu_full (period expected returns).
    if min_ret_ann_raw is not None:
        # If user sets a target but does not specify mode, default to "soft" to avoid hard infeasibility
        if min_ret_mode in {"", "none", "null"}:
            min_ret_mode = "soft"

        if not isinstance(min_ret_ann_raw, numbers.Real):
            try:
                min_ret_ann_raw = float(min_ret_ann_raw)
            except (TypeError, ValueError) as e:
                raise ValueError("min_expected_return_annual must be a number.") from e

        min_ret_ann = float(min_ret_ann_raw)
        min_ret_target_ann = min_ret_ann
        if min_ret_ann <= -0.99:
            raise ValueError("min_expected_return_annual must be > -0.99")

        min_ret_target_p = annual_to_period_rate(min_ret_ann, ppy)
        # Excess-over-cash means: (mu - r_cash) dot w >= target
        # Since sum(w)=1, this is equivalent to mu_full@w - r_cash >= target.
        min_ret_expr = (mu_full - cash_period_return) @ w if min_ret_is_excess else (mu_full @ w)

        if min_ret_mode in {"hard", "constraint"}:
            c = min_ret_expr >= min_ret_target_p
            constraints.append(c)
            min_return_constraints.append(c)
        elif min_ret_mode in {"soft", "penalty"}:
            min_ret_slack = cp.Variable(nonneg=True, name="min_return_shortfall")
            c = min_ret_expr + min_ret_slack >= min_ret_target_p
            constraints.append(c)
            min_return_constraints.append(c)
        else:
            logger.warning("Unknown min_expected_return_mode=%r; ignoring minimum return constraint.", min_ret_mode)
            min_ret_target_p = None
            min_ret_expr = None
            min_ret_slack = None
            min_ret_target_ann = float("nan")

    if cash_relax_enabled:
        cash_band = budgets.get("CASH", {}) or {}
        if cash_band:
            cash_max_slack = cp.Variable(nonneg=True, name="cash_max_slack")
            constraints.append(cash_max_slack <= cash_relax_max_extra)

    # -----------------------
    # Region sleeve budgets
    # Tier-1 long/short: allow NET vs LONG vs GROSS exposure budgets.
    # -----------------------
    def _exposure_expr(indices: List[int], sleeve: str) -> Any:
        if not is_long_short or region_budget_mode == "net":
            # net exposure
            return cp.sum(w[indices]) if indices else 0.0

        # For cash, exposure is just the net cash weight.
        if sleeve.upper() == "CASH":
            return w[-1]

        assert w_plus is not None and w_minus is not None
        if region_budget_mode == "long":
            return cp.sum(w_plus[indices]) if indices else 0.0
        if region_budget_mode == "gross":
            return cp.sum(w_plus[indices] + w_minus[indices]) if indices else 0.0
        # fallback
        return cp.sum(w[indices]) if indices else 0.0

    def add_band_expr(expr: Any, band: Dict[str, Any], name: str) -> None:
        if not band:
            return
        mn = float(band.get("min", 0.0))
        mx = float(band.get("max", 1.0))
        constraints.append(expr >= mn)
        if name.upper() == "CASH" and cash_max_slack is not None:
            constraints.append(expr <= mx + cash_max_slack)
        else:
            constraints.append(expr <= mx)

    add_band_expr(_exposure_expr(idx_us, "US"), budgets.get("US", {}) or {}, "US")
    add_band_expr(_exposure_expr(idx_foreign, "FOREIGN"), budgets.get("FOREIGN", {}) or {}, "FOREIGN")
    add_band_expr(_exposure_expr(idx_cash, "CASH"), budgets.get("CASH", {}) or {}, "CASH")

    # Optional: foreign region group budgets
    fr_budgets = acfg.get("foreign_region_budgets", {}) or {}
    if fr_budgets:
        for gname, band in fr_budgets.items():
            g_idx = [i for i in idx_foreign if str(assets.iloc[i].get("RegionGroup", "")) == str(gname)]
            if not g_idx:
                continue
            add_band_expr(_exposure_expr(g_idx, "FOREIGN"), band or {}, f"FOREIGN:{gname}")

    # Sector caps (risk-control only, preferences go into BL alpha)
    scfg = cfg.get("sector", {}) or {}
    sector_band = float(scfg.get("sector_cap_band", 0.05))
    bench_sector_weights: Dict[str, float] = scfg.get("benchmark_sector_weights", {}) or {}

    # infer benchmark sector weights if not provided
    if not bench_sector_weights:
        # based on selected US universe count (practical fallback)
        us = assets.iloc[idx_us].copy()
        if len(us) > 0:
            if is_long_short and "LS_Book" in us.columns:
                # Tier-1: build benchmark weights from LONG book only (avoids infeasible "min" constraints
                # when a sector exists only in the short book).
                long_mask = us["LS_Book"].astype(str).str.upper() == "LONG"
                base = us.loc[long_mask] if long_mask.any() else us
            else:
                base = us
            counts = base["SectorName"].value_counts(normalize=True).to_dict()
            bench_sector_weights = {k: float(v) for k, v in counts.items()}

    # Apply sector caps on the US sleeve only
    if not is_long_short:
        # long-only: symmetric band around benchmark (net==long)
        us_total = cp.sum(w[idx_us]) if idx_us else 0.0
        for sec_name, bench_w in bench_sector_weights.items():
            sec_idx = [i for i in idx_us if str(assets.iloc[i].get("SectorName", "")) == str(sec_name)]
            if not sec_idx:
                continue
            lo = max(0.0, float(bench_w) - sector_band)
            hi = min(1.0, float(bench_w) + sector_band)
            constraints.append(cp.sum(w[sec_idx]) >= lo * us_total)
            constraints.append(cp.sum(w[sec_idx]) <= hi * us_total)
    else:
        # long/short Tier-1: apply MAX-ONLY caps on the LONG book (prevents infeasible min constraints).
        assert w_plus is not None
        us_long_total = cp.sum(w_plus[idx_us]) if idx_us else 0.0
        for sec_name, bench_w in bench_sector_weights.items():
            sec_idx = [i for i in idx_us if str(assets.iloc[i].get("SectorName", "")) == str(sec_name)]
            if not sec_idx:
                continue
            hi = min(1.0, float(bench_w) + sector_band)
            constraints.append(cp.sum(w_plus[sec_idx]) <= hi * us_long_total)

    # Stage 12D macro stock target constraints.
    # Apply as max bands by default: min bands are usually infeasible after cardinality preselection
    # because many target industries will not have a selected stock in a 25-30 name book.
    stage12d_cfg = cfg.get("macro_optimizer_integration", {}) or {}
    target_cfg = stage12d_cfg.get("stock_targets", {}) or {}
    stage12d_targets = cfg.get("_stage12d_targets", {}) or {}
    if bool(stage12d_cfg.get("enabled", False)) and bool(target_cfg.get("enabled", False)) and idx_us:
        enforce_min = bool(target_cfg.get("enforce_min_bands", False))
        enforce_industry_max = bool(target_cfg.get("enforce_industry_max", True))
        enforce_sector_max = bool(target_cfg.get("enforce_sector_max", True))
        normalize_caps = bool(target_cfg.get("normalize_caps_to_available_groups", True))
        buffer = max(0.0, float(target_cfg.get("max_weight_buffer", 0.0)))

        if is_long_short and w_plus is not None:
            us_target_total = cp.sum(w_plus[idx_us])

            def _group_expr(group_idx: List[int]) -> Any:
                return cp.sum(w_plus[group_idx]) if group_idx else 0.0
        else:
            us_target_total = cp.sum(w[idx_us])

            def _group_expr(group_idx: List[int]) -> Any:
                return cp.sum(w[group_idx]) if group_idx else 0.0

        industry_targets = stage12d_targets.get("industry")
        if enforce_industry_max and isinstance(industry_targets, pd.DataFrame) and not industry_targets.empty:
            represented = {
                str(assets.iloc[i].get("IndustryName", "")).strip()
                for i in idx_us
                if str(assets.iloc[i].get("IndustryName", "")).strip()
            }
            industry_cap_sum = 0.0
            if normalize_caps:
                for _, row in industry_targets.iterrows():
                    name = str(row.get("industry_name", "")).strip()
                    if name not in represented:
                        continue
                    mx = pd.to_numeric(pd.Series([row.get("max_weight", np.nan)]), errors="coerce").iloc[0]
                    if pd.notna(mx):
                        industry_cap_sum += max(0.0, float(mx))
            industry_scale = (1.0 / industry_cap_sum) if normalize_caps and industry_cap_sum > 1e-12 and industry_cap_sum < 1.0 else 1.0
            for _, row in industry_targets.iterrows():
                industry_name = str(row.get("industry_name", "")).strip()
                if not industry_name:
                    continue
                group_idx = [
                    i for i in idx_us
                    if str(assets.iloc[i].get("IndustryName", "")).strip() == industry_name
                ]
                if not group_idx:
                    continue
                mx = pd.to_numeric(pd.Series([row.get("max_weight", np.nan)]), errors="coerce").iloc[0]
                if pd.notna(mx):
                    constraints.append(_group_expr(group_idx) <= min(1.0, float(mx) * industry_scale + buffer) * us_target_total)
                if enforce_min:
                    mn = pd.to_numeric(pd.Series([row.get("min_weight", np.nan)]), errors="coerce").iloc[0]
                    if pd.notna(mn):
                        constraints.append(_group_expr(group_idx) >= max(0.0, float(mn)) * us_target_total)

        sector_targets = stage12d_targets.get("sector")
        if enforce_sector_max and isinstance(sector_targets, pd.DataFrame) and not sector_targets.empty:
            represented = {
                str(assets.iloc[i].get("SectorName", "")).strip()
                for i in idx_us
                if str(assets.iloc[i].get("SectorName", "")).strip()
            }
            sector_cap_sum = 0.0
            if normalize_caps:
                for _, row in sector_targets.iterrows():
                    name = str(row.get("sector_name", "")).strip()
                    if name not in represented:
                        continue
                    mx = pd.to_numeric(pd.Series([row.get("max_weight", np.nan)]), errors="coerce").iloc[0]
                    if pd.notna(mx):
                        sector_cap_sum += max(0.0, float(mx))
            sector_scale = (1.0 / sector_cap_sum) if normalize_caps and sector_cap_sum > 1e-12 and sector_cap_sum < 1.0 else 1.0
            for _, row in sector_targets.iterrows():
                sector_name = str(row.get("sector_name", "")).strip()
                if not sector_name:
                    continue
                group_idx = [
                    i for i in idx_us
                    if str(assets.iloc[i].get("SectorName", "")).strip() == sector_name
                ]
                if not group_idx:
                    continue
                mx = pd.to_numeric(pd.Series([row.get("max_weight", np.nan)]), errors="coerce").iloc[0]
                if pd.notna(mx):
                    constraints.append(_group_expr(group_idx) <= min(1.0, float(mx) * sector_scale + buffer) * us_target_total)
                if enforce_min:
                    mn = pd.to_numeric(pd.Series([row.get("min_weight", np.nan)]), errors="coerce").iloc[0]
                    if pd.notna(mn):
                        constraints.append(_group_expr(group_idx) >= max(0.0, float(mn)) * us_target_total)

    # Cluster caps (very effective, transparent)
    if bool(dcfg.get("use_cluster_caps", True)) and n_risky >= 3:
        # correlation for clustering: average Pearson + Kendall base
        corrP = cov_to_corr(cov_scenarios[0].cov)
        corrK = cov_to_corr(cov_scenarios[1].cov) if len(cov_scenarios) > 1 else corrP
        corrC = 0.5 * (corrP + corrK)
        max_clusters = int((dcfg.get("clustering", {}) or {}).get("max_clusters", 8))
        link_method = str((dcfg.get("clustering", {}) or {}).get("linkage", "average"))
        clusters = cluster_assets(corrC, tickers_risky, max_clusters=max_clusters, linkage_method=link_method)

        cap = float(dcfg.get("cluster_cap_long_short" if is_long_short else "cluster_cap_long_only", 0.25))
        # Apply to risky assets only (exclude cash); for long/short use abs
        for cl in sorted(set(clusters.values())):
            cl_idx = [i for i, t in enumerate(tickers_risky) if clusters[t] == cl]
            if len(cl_idx) <= 1:
                continue
            if is_long_short:
                # Tier-1: with split variables, abs(w_i) == w_plus_i + w_minus_i for sign-restricted names.
                assert w_plus is not None and w_minus is not None
                constraints.append(cp.sum(w_plus[cl_idx] + w_minus[cl_idx]) <= cap)
            else:
                constraints.append(cp.sum(w[cl_idx]) <= cap)

    # Long/short gross exposure constraint
    borrow_cost_term = 0.0
    if is_long_short:
        gross_limit = float(ls_cfg.get("gross_limit", 1.6))
        assert w_plus is not None and w_minus is not None
        constraints.append(cp.sum(w_plus + w_minus) <= gross_limit)

        borrow_annual_us = float(ls_cfg.get("borrow_cost_annual", 0.02))
        borrow_p_us = annual_to_period_rate(borrow_annual_us, ppy)
        borrow_cost_term = borrow_p_us * cp.sum(w_minus[idx_us]) if idx_us else 0.0
        if idx_foreign:
            borrow_annual_fx = float(ls_cfg.get("borrow_cost_annual_foreign", borrow_annual_us))
            borrow_p_fx = annual_to_period_rate(borrow_annual_fx, ppy)
            borrow_cost_term = borrow_cost_term + (borrow_p_fx * cp.sum(w_minus[idx_foreign]))

    # Turnover controls
    if prev_weights is not None:
        w_prev = np.zeros(n, dtype=float)
        for i, t in enumerate(tickers_risky):
            w_prev[i] = float(prev_weights.get(t, 0.0))
        w_prev[-1] = float(prev_weights.get(ucfg.get("cash_symbol", "CASH"), 0.0))
        turnover = cp.norm1(w - w_prev)
        # Tier-1: if the universe was pruned, add constant turnover for dropped names
        turnover_total = turnover + float(turnover_extra)
        if max_turnover is not None:
            constraints.append(turnover_total <= float(max_turnover))
        turnover_term = turnover_total
    else:
        turnover_term = 0.0

    # Risk term
    if robust_mode == "worst_case":
        t_var = cp.Variable(nonneg=True)
        for sc in cov_scenarios:
            cov_full = embed_cov_including_cash(sc.cov, n_risky)
            constraints.append(cp.quad_form(w, cov_full) <= t_var)
        risk_term = t_var
    elif robust_mode == "single":
        cov_full = embed_cov_including_cash(cov_scenarios[0].cov, n_risky)
        risk_term = cp.quad_form(w, cov_full)
    else:
        # average
        cov_avg = np.mean([sc.cov for sc in cov_scenarios[:2]], axis=0) if len(cov_scenarios) >= 2 else cov_scenarios[0].cov
        cov_full = embed_cov_including_cash(cov_avg, n_risky)
        risk_term = cp.quad_form(w, cov_full)

    # Concentration penalty applies to risky assets only; cash is governed by
    # its own allocation band and should not be penalized like a concentrated
    # security position.
    hhi = cp.sum_squares(w[:n_risky])

    # Objective (maximize concave)
    # mu_full @ w - risk_aversion * risk - hhi_penalty * sumsq - turnover_penalty * turnover - borrow costs
    obj_expr = (
        mu_full @ w
        - risk_aversion * risk_term
        - hhi_penalty * hhi
        - turnover_penalty * turnover_term
        - borrow_cost_term
    )
    if cash_max_slack is not None and cash_relax_penalty > 0.0:
        obj_expr = obj_expr - cash_relax_penalty * cash_max_slack
    # Soft minimum-return enforcement: penalize any shortfall heavily.
    if min_ret_slack is not None:
        obj_expr = obj_expr - min_ret_penalty * min_ret_slack

    obj = cp.Maximize(obj_expr)

    prob = cp.Problem(obj, constraints)

    # Solve
    try:
        prob.solve(solver=solver, verbose=False)
    except Exception as primary_exc:
        if str(solver).upper() == "SCS":
            raise RuntimeError(
                f"SCS failed for {portfolio_name}: {primary_exc}"
            ) from primary_exc
        logger.warning("Solver %s failed (%s). Falling back to SCS.", solver, str(primary_exc))
        try:
            prob.solve(solver="SCS", verbose=False)
        except Exception as fallback_exc:
            raise RuntimeError(
                f"Both {solver} and SCS failed for {portfolio_name}: "
                f"{type(fallback_exc).__name__}: {fallback_exc}"
            ) from fallback_exc

    if w.value is None:
        status = str(prob.status)
        if status in {"infeasible", "infeasible_inaccurate"}:
            raise OptimizationInfeasibleError(portfolio_name, status)
        raise RuntimeError(f"Optimization failed for {portfolio_name}. Status: {status}")

    w_opt = np.array(w.value).reshape(-1)
    w_opt[np.abs(w_opt) < 1e-10] = 0.0

    # Metrics (annualized approximations)
    cov_metric = np.mean([sc.cov for sc in cov_scenarios[:2]], axis=0) if len(cov_scenarios) >= 2 else cov_scenarios[0].cov
    cov_full_metric = embed_cov_including_cash(cov_metric, n_risky)

    mu_p = float(mu_full @ w_opt)
    vol_p = float(math.sqrt(max(0.0, w_opt.T @ cov_full_metric @ w_opt)))

    # Tier-1 reporting metric: net geometric annual return.
    # Keep optimizer objective unchanged; only reported exp_return_ann is adjusted.
    borrow_cost_p_realized = 0.0
    if is_long_short and idx_us:
        borrow_annual = float(ls_cfg.get("borrow_cost_annual", 0.02))
        borrow_p = annual_to_period_rate(borrow_annual, ppy)
        us_weights = np.asarray(w_opt[idx_us], dtype=float)
        short_us_gross = np.maximum(-us_weights, 0.0)
        borrow_cost_p_realized = float(borrow_p * short_us_gross.sum())
    if is_long_short and idx_foreign:
        borrow_annual_fx = float(ls_cfg.get("borrow_cost_annual_foreign", ls_cfg.get("borrow_cost_annual", 0.02)))
        borrow_p_fx = annual_to_period_rate(borrow_annual_fx, ppy)
        fx_weights = np.asarray(w_opt[idx_foreign], dtype=float)
        short_fx_gross = np.maximum(-fx_weights, 0.0)
        borrow_cost_p_realized += float(borrow_p_fx * short_fx_gross.sum())

    mu_p_net = float(mu_p - borrow_cost_p_realized)
    geo_log_p = float(mu_p_net - 0.5 * (vol_p ** 2))
    exp_arg = float(ppy) * geo_log_p
    exp_arg = max(min(exp_arg, 700.0), -700.0)
    mu_ann = float(math.exp(exp_arg) - 1.0)
    vol_ann = float(vol_p * math.sqrt(ppy))

    cash_ann = float((1.0 + cash_period_return) ** ppy - 1.0)
    sharpe = float((mu_ann - cash_ann) / vol_ann) if vol_ann > 1e-12 else float("nan")

    # NOTE: cvxpy type stubs sometimes expose `Problem.value` as `numbers.Number | None`,
    # which Pylance won't accept directly in `float(...)`. At runtime it's a real scalar
    # when not None, so we safely cast for the type-checker.
    obj_val = prob.value
    metrics: Dict[str, Any] = {
        "exp_return_ann": mu_ann,
        "vol_ann": vol_ann,
        "sharpe_ann": sharpe,
        "objective_value": float(cast(float, obj_val)) if obj_val is not None else float("nan"),
    }
    metrics.update(dispersion_diag)

    us_w = np.asarray(w_opt[idx_us], dtype=float) if idx_us else np.zeros(0, dtype=float)
    fx_w = np.asarray(w_opt[idx_foreign], dtype=float) if idx_foreign else np.zeros(0, dtype=float)
    cash_w = float(w_opt[-1])
    us_mu = np.asarray(mu_full[idx_us], dtype=float) if idx_us else np.zeros(0, dtype=float)
    fx_mu = np.asarray(mu_full[idx_foreign], dtype=float) if idx_foreign else np.zeros(0, dtype=float)
    metrics.update({
        "exp_return_contrib_period_us_net": float(us_mu @ us_w) if idx_us else 0.0,
        "exp_return_contrib_period_foreign_net": float(fx_mu @ fx_w) if idx_foreign else 0.0,
        "exp_return_contrib_period_cash": float(cash_period_return * cash_w),
        "net_exposure_us": float(us_w.sum()) if idx_us else 0.0,
        "net_exposure_foreign": float(fx_w.sum()) if idx_foreign else 0.0,
        "cash_weight": float(cash_w),
        "risky_gross_exposure": float(np.abs(w_opt[:n_risky]).sum()),
        "portfolio_sum_weight": float(w_opt.sum()),
    })
    if is_long_short:
        us_long = np.maximum(us_w, 0.0)
        us_short = np.minimum(us_w, 0.0)
        fx_long = np.maximum(fx_w, 0.0)
        fx_short = np.minimum(fx_w, 0.0)
        metrics.update({
            "gross_exposure_us_long": float(us_long.sum()),
            "gross_exposure_us_short": float((-us_short).sum()),
            "gross_exposure_foreign_long": float(fx_long.sum()),
            "gross_exposure_foreign_short": float((-fx_short).sum()),
            "exp_return_contrib_period_us_long": float(us_mu @ us_long) if idx_us else 0.0,
            "exp_return_contrib_period_us_short": float(us_mu @ us_short) if idx_us else 0.0,
            "exp_return_contrib_period_foreign_long": float(fx_mu @ fx_long) if idx_foreign else 0.0,
            "exp_return_contrib_period_foreign_short": float(fx_mu @ fx_short) if idx_foreign else 0.0,
        })

    if cash_max_slack is not None:
        slack_val = float(np.asarray(cash_max_slack.value).item()) if cash_max_slack.value is not None else float("nan")
        metrics.update({
            "cash_max_relaxation_enabled": bool(cash_relax_enabled),
            "cash_max_slack": slack_val,
            "cash_budget_relaxation_used": bool(np.isfinite(slack_val) and slack_val > 1e-10),
        })
        if np.isfinite(slack_val) and slack_val > 1e-10:
            logger.warning("%s: CASH max relaxed by +%.4f (elastic slack used).", portfolio_name, slack_val)

    # Minimum expected return diagnostics (if enabled)
    if min_ret_target_p is not None and min_ret_expr is not None:
        achieved_total_p = float(mu_full @ w_opt)
        achieved_p = achieved_total_p - cash_period_return if min_ret_is_excess else achieved_total_p
        achieved_ann = float((1.0 + achieved_p) ** ppy - 1.0)

        # Shortfall (period units)
        slack_val = getattr(min_ret_slack, "value", None) if min_ret_slack is not None else None
        if slack_val is not None:
            shortfall_p = float(np.asarray(slack_val).item())
        else:
            shortfall_p = max(0.0, float(min_ret_target_p - achieved_p))
        shortfall_ann = float((1.0 + shortfall_p) ** ppy - 1.0) if shortfall_p > 0 else 0.0

        metrics.update({
            "min_return_mode": str(min_ret_mode),
            "min_return_is_excess_over_cash": bool(min_ret_is_excess),
            "min_return_target_ann": min_ret_target_ann,
            "min_return_achieved_ann": achieved_ann,
            "min_return_shortfall_ann": shortfall_ann,
            "min_return_shortfall_period": shortfall_p,
        })

        # Feasibility diagnostic: if we missed target, compute max achievable expected return
        if achieved_p + 1e-12 < float(min_ret_target_p):
            try:
                min_constraint_ids = {id(c) for c in min_return_constraints}
                constraints_no_min = [c for c in constraints if id(c) not in min_constraint_ids]

                prob_max = cp.Problem(cp.Maximize(min_ret_expr), constraints_no_min)
                try:
                    prob_max.solve(solver=solver, verbose=False)
                except Exception:
                    prob_max.solve(solver="SCS", verbose=False)
                if min_ret_expr.value is not None:
                    max_p = float(np.asarray(min_ret_expr.value).item())
                    max_ann = float((1.0 + max_p) ** ppy - 1.0)
                    metrics.update({
                        "min_return_max_achievable_ann": max_ann,
                        "min_return_feasible": bool(max_p + 1e-12 >= float(min_ret_target_p)),
                    })
            except Exception as e:
                logger.warning("Failed min-return feasibility diagnostic: %s", str(e))

    # Return series (include cash symbol)
    cash_symbol = ucfg.get("cash_symbol", "CASH")
    out_idx = tickers_risky + [cash_symbol]
    return pd.Series(w_opt, index=out_idx), metrics


def embed_cov_including_cash(cov_risky: np.ndarray, n_risky: int) -> np.ndarray:
    """
    Expand risky covariance to include cash as last dimension with zero var/cov.
    """
    n = n_risky + 1
    cov = np.zeros((n, n), dtype=float)
    cov[:n_risky, :n_risky] = cov_risky
    return cov


def cov_to_corr(cov: np.ndarray) -> np.ndarray:
    cov = symmetrize(cov)
    if not np.isfinite(cov).all():
        cov = np.nan_to_num(cov, nan=0.0, posinf=0.0, neginf=0.0)

    diag = np.diag(cov)
    d = np.sqrt(np.maximum(diag, 0.0))
    invd = np.zeros_like(d)
    mask = d > 0.0
    invd[mask] = 1.0 / d[mask]

    corr = cov * np.outer(invd, invd)
    corr = symmetrize(corr)
    corr = np.clip(corr, -1.0, 1.0)
    np.fill_diagonal(corr, 1.0)

    if not np.all(mask):
        zero_idx = np.where(~mask)[0]
        corr[zero_idx, :] = 0.0
        corr[:, zero_idx] = 0.0
        np.fill_diagonal(corr, 1.0)
    return corr


def compute_weight_bands(
    portfolio_name: str,
    assets: pd.DataFrame,
    rets: pd.DataFrame,
    mu_risky: np.ndarray,
    cov_scenarios: List[CovScenario],
    cfg: Dict[str, Any],
    ppy: int,
    cash_period_return: float,
    prev_weights: Optional[pd.Series],
    is_long_short: bool,
    turnover_extra: float = 0.0,
    allow_cash_budget_relaxation: bool = False,
) -> Tuple[pd.Series, pd.Series]:
    """
    Compute low/high bands by re-optimizing under each scenario covariance (single-cov mode),
    then taking quantiles across solutions.

    Bands are what you asked for: driven by Pearson and Kendall tau dependence (plus bootstraps if enabled).
    """
    bq = (cfg.get("output", {}) or {}).get("bands_quantiles", [0.05, 0.95])
    if not isinstance(bq, (list, tuple)) or len(bq) != 2:
        raise ValueError("output.bands_quantiles must be a list/tuple of length 2, e.g. [0.05, 0.95].")
    q_lo, q_hi = float(bq[0]), float(bq[1])

    out_cfg = cfg.get("output", {}) or {}
    # Tier-1 default: BL posterior depends on Sigma, so recompute mu per scenario.
    recompute_mu = bool(out_cfg.get("bands_recompute_bl_per_scenario", True))

    # Temporarily force single-mode to isolate scenario effect
    cfg_single = copy.deepcopy(cfg)
    cfg_single["risk"]["robust_mode"] = "single"

    w_solutions = []
    skipped_scenarios: List[str] = []
    for sc in cov_scenarios:
        if recompute_mu:
            # Recompute BL posterior under THIS scenario covariance
            mu_sc, _ = build_bl_inputs(
                assets=assets,
                cov_base=sc.cov,
                rets=rets,
                cfg=cfg,
                ppy=ppy,
                cash_period_return=cash_period_return,
            )
        else:
            mu_sc = mu_risky

        try:
            w_sc, _ = solve_portfolio(
                portfolio_name=f"{portfolio_name}__{sc.name}",
                assets=assets,
                rets=rets,
                mu_risky=mu_sc,
                cov_scenarios=[sc],
                cfg=cfg_single,
                ppy=ppy,
                cash_period_return=cash_period_return,
                prev_weights=prev_weights,
                is_long_short=is_long_short,
                turnover_extra=turnover_extra,
                allow_cash_budget_relaxation=allow_cash_budget_relaxation,
            )
            w_solutions.append(w_sc)
        except RuntimeError as e:
            # Bootstrap scenarios can be infeasible due to extreme covariance estimates;
            # skip them gracefully and log a warning.
            if "infeasible" in str(e).lower():
                skipped_scenarios.append(sc.name)
                logger.warning(
                    f"compute_weight_bands: skipping infeasible scenario '{sc.name}' for {portfolio_name}"
                )
            else:
                raise  # Re-raise non-infeasibility errors

    if skipped_scenarios:
        logger.warning(
            f"compute_weight_bands: {len(skipped_scenarios)} scenario(s) skipped for {portfolio_name}: "
            f"{skipped_scenarios}"
        )

    if not w_solutions:
        raise RuntimeError(
            f"compute_weight_bands: ALL scenarios infeasible for {portfolio_name}. "
            f"Consider relaxing constraints (min_expected_return_annual, sector_cap_band) "
            f"or disabling bootstrap."
        )

    W = pd.concat(w_solutions, axis=1)
    low = W.quantile(q_lo, axis=1)
    high = W.quantile(q_hi, axis=1)
    return low, high


# --------------------------
# End-to-end builder
# --------------------------
def build_assets_table(
    us_df: pd.DataFrame,
    sector_df: pd.DataFrame,
    foreign_df: pd.DataFrame,
    cfg: Dict[str, Any],
    portfolio_type: str
) -> pd.DataFrame:
    """
    Build a unified asset metadata table for risky assets:
    columns include: Ticker, Company, Sleeve, Rating, FinalScore, SectorName, SignalScore, SignalScoreZ, etc.
    """
    scfg = cfg.get("sector", {}) or {}
    map_stock_sector = scfg.get("stock_to_sectorname", {}) or {}

    # Sector rotation lookup
    sec_scorepct = sector_df.set_index("SectorName")["ScorePct"].to_dict()
    sec_state = sector_df.set_index("SectorName")["State"].to_dict()

    # US assets
    us = us_df.copy()
    if "Company" not in us.columns:
        us["Company"] = ""
    else:
        us["Company"] = us["Company"].fillna("").astype(str).str.strip()
    us["SectorName"] = us["sector"].map(lambda x: map_stock_sector.get(str(x), str(x)))
    us["IndustryName"] = us["industry"].fillna("").astype(str).str.strip() if "industry" in us.columns else ""
    us["IndustryAggregateName"] = (
        us["industry_aggregate"].fillna("").astype(str).str.strip() if "industry_aggregate" in us.columns else ""
    )
    us["SectorScorePct"] = us["SectorName"].map(lambda s: sec_scorepct.get(str(s), np.nan))
    us["SectorState"] = us["SectorName"].map(lambda s: sec_state.get(str(s), "Unknown"))
    mcfg = cfg.get("macro_optimizer_integration", {}) or {}
    er_cfg = mcfg.get("expected_return", {}) or {}
    signal_col = str(er_cfg.get("stock_signal_column", "FinalScore")).strip() or "FinalScore"
    if bool(mcfg.get("enabled", False)) and signal_col in us.columns:
        us["SignalScore"] = pd.to_numeric(us[signal_col], errors="coerce").fillna(us["FinalScore"]).astype(float)
    else:
        us["SignalScore"] = us["FinalScore"].astype(float)
    us = us.rename(columns={"sector": "SectorRaw"})
    us["AssetType"] = "US_STOCK"
    if "LS_Book" not in us.columns:
        us["LS_Book"] = ""
    for c in ("NextEarningsDate", "EarningsDaysAhead", "EarningsDaysAheadAsOf", "EarningsFilterNote"):
        if c not in us.columns:
            us[c] = ""

    # Foreign assets
    fx = foreign_df.copy()
    fx["Rating"] = "FOREIGN"
    fx["SectorName"] = "FOREIGN"
    fx["IndustryName"] = "FOREIGN"
    fx["IndustryAggregateName"] = "FOREIGN"
    fx["SectorScorePct"] = np.nan
    fx["SectorState"] = "FOREIGN"
    fx["SignalScore"] = fx["Score"].astype(float)
    fx["FinalScore"] = fx["SignalScore"]
    fx["AssetType"] = "FOREIGN_ETF"
    fx["LS_Book"] = "LONG"
    for c in ("NextEarningsDate", "EarningsDaysAhead", "EarningsDaysAheadAsOf", "EarningsFilterNote"):
        if c not in fx.columns:
            fx[c] = ""
    if "Company" not in fx.columns:
        fx["Company"] = fx["MarketName"] if "MarketName" in fx.columns else ""
    else:
        fx["Company"] = fx["Company"].fillna("").astype(str).str.strip()
        if "MarketName" in fx.columns:
            empty = fx["Company"] == ""
            fx.loc[empty, "Company"] = fx.loc[empty, "MarketName"]

    # Combine - include _FullUniverseZ if pre-computed (signal_zscore_scope=full)
    us_cols = [
        "Ticker", "Company", "Sleeve", "AssetType", "Rating", "FinalScore", "SignalScore",
        "SectorName", "IndustryAggregateName", "IndustryName", "SectorScorePct", "SectorState", "LS_Book",
        "NextEarningsDate", "EarningsDaysAhead", "EarningsDaysAheadAsOf", "EarningsFilterNote",
    ]
    for optional_col in ("ExpectedAlphaAnnual", "ScoreConfidence", "SourcePipeline"):
        if optional_col in us.columns and optional_col not in us_cols:
            us_cols.append(optional_col)
    if "_FullUniverseZ" in us.columns:
        us_cols.append("_FullUniverseZ")
    fx_cols = [
        "Ticker", "Company", "Sleeve", "AssetType", "Rating", "FinalScore", "SignalScore",
        "SectorName", "IndustryAggregateName", "IndustryName", "SectorScorePct", "SectorState",
        "RegionGroup", "MarketName", "LS_Book",
        "NextEarningsDate", "EarningsDaysAhead", "EarningsDaysAheadAsOf", "EarningsFilterNote",
    ]
    assets = pd.concat(
        [
            us[us_cols].copy(),
            fx[fx_cols].copy()
        ],
        axis=0,
        ignore_index=True
    )

    assets["Ticker"] = assets["Ticker"].astype(str).str.upper().str.strip()
    dup_mask = assets["Ticker"].duplicated(keep="first")
    if dup_mask.any():
        dupes = sorted(assets.loc[dup_mask, "Ticker"].unique().tolist())
        logger.warning(
            "Duplicate tickers detected in asset universe; keeping first occurrence only. Duplicates: %s",
            ", ".join(dupes),
        )
        assets = assets.drop_duplicates(subset=["Ticker"], keep="first").reset_index(drop=True)

    # Normalize core signal: use pre-computed full-universe z if available (for US stocks)
    if "_FullUniverseZ" in assets.columns:
        # Use full-universe z for US, compute local z for FOREIGN
        assets["SignalScoreZ"] = assets["_FullUniverseZ"]
        fx_mask = assets["Sleeve"] == "FOREIGN"
        if fx_mask.any():
            assets.loc[fx_mask, "SignalScoreZ"] = zscore(assets.loc[fx_mask, "SignalScore"].astype(float)).values
        assets.drop(columns=["_FullUniverseZ"], inplace=True)
    else:
        assets["SignalScoreZ"] = zscore(assets["SignalScore"].astype(float))

    # Add sector z-score (for US only) if present
    if "SectorScorePct" in assets.columns:
        # compute z across available sectors only
        sec_vals = assets.loc[assets["Sleeve"] == "US", "SectorScorePct"].dropna()
        # simpler: compute z per row directly
        assets["SectorScoreZ"] = 0.0
        if len(sec_vals) > 0:
            # build sector mean z per sector name
            sec_tbl = (
                assets.loc[assets["Sleeve"] == "US", ["SectorName", "SectorScorePct"]]
                .dropna()
                .drop_duplicates()
                .set_index("SectorName")["SectorScorePct"]
            )
            sec_z = zscore(sec_tbl)
            sec_z_dict = sec_z.to_dict()
            assets.loc[assets["Sleeve"] == "US", "SectorScoreZ"] = assets.loc[assets["Sleeve"] == "US", "SectorName"].map(
                lambda s: float(sec_z_dict.get(str(s), 0.0))
            )
        else:
            assets["SectorScoreZ"] = 0.0

    # Foreign score z (optional)
    if "MarketName" in assets.columns:
        fx_mask = assets["Sleeve"] == "FOREIGN"
        if fx_mask.any():
            assets.loc[fx_mask, "ForeignScoreZ"] = zscore(assets.loc[fx_mask, "SignalScore"].astype(float)).values
        else:
            assets["ForeignScoreZ"] = 0.0
    else:
        assets["ForeignScoreZ"] = 0.0

    # Ensure RegionGroup exists for all (for constraints)
    if "RegionGroup" not in assets.columns:
        assets["RegionGroup"] = ""

    return assets


def clean_returns_df(rets: pd.DataFrame, cfg: Dict[str, Any]) -> pd.DataFrame:
    rc = _require_cfg_section(cfg, "returns")
    min_rows = int(rc.get("min_history_rows", 60))
    max_nan = float(rc.get("max_nan_frac", 0.05))
    drop_insufficient = bool(rc.get("drop_insufficient_history", True))
    ffill_max_periods = max(0, int(rc.get("ffill_max_periods", 5)))

    # Drop columns with too many NaNs (ignore leading NaNs for partial histories).
    valid_counts = rets.notna().sum()
    nan_frac: Dict[str, float] = {}
    for col in rets.columns:
        s = rets[col]
        if s.notna().any():
            first = s.first_valid_index()
            s2 = s.loc[first:] if first is not None else s
            nan_frac[str(col)] = float(s2.isna().mean())
        else:
            nan_frac[str(col)] = 1.0

    keep_cols = [c for c in rets.columns if nan_frac.get(str(c), 1.0) <= max_nan]
    if drop_insufficient:
        keep_cols = [c for c in keep_cols if int(valid_counts.get(c, 0)) >= min_rows]
    else:
        keep_cols = [c for c in keep_cols if int(valid_counts.get(c, 0)) > 0]

    rets = rets[keep_cols].copy()
    if rets.empty:
        raise ValueError("No tickers left after returns cleaning.")

    # Forward fill small gaps then drop remaining NaNs
    if ffill_max_periods > 0:
        rets = rets.ffill(limit=ffill_max_periods).dropna()
    else:
        rets = rets.dropna()

    # Drop zero-price/delisting artifacts that become infinite returns before
    # they can contaminate winsorization and covariance estimation.
    rets = rets.replace([np.inf, -np.inf], np.nan).dropna()

    if len(rets) < min_rows:
        raise ValueError(f"Not enough return history after cleaning: {len(rets)} rows < {min_rows}")

    # Winsorize
    win = rc.get("winsorize", {}) or {}
    if bool(win.get("enabled", True)):
        rets = winsorize_df(rets, float(win.get("lower_q", 0.01)), float(win.get("upper_q", 0.99)))

    return rets


def run_end_to_end_from_cfg(
    cfg: Dict[str, Any],
    *,
    cfg_path: Optional[Path] = None,
    prices_by_ticker: Optional[Dict[str, pd.DataFrame]] = None,
    provider: Optional[ReturnsDataProvider] = None,
) -> Dict[str, OptResult]:
    root_cfg = cfg
    cfg = _get_tier1_cfg(cfg)
    cfg_path_obj = Path(cfg_path).expanduser().resolve() if cfg_path is not None else None
    cfg["_cfg_path"] = str(cfg_path_obj) if cfg_path_obj is not None else ""

    out_dir_path = _resolve_output_dir(cfg, cfg_path_obj)
    if out_dir_path is None:
        logger.warning("No output directory configured; writing optimizer outputs to ./out")
        out_dir_path = Path("out")
    ensure_dir(str(out_dir_path))
    out_dir = str(out_dir_path)
    out_cfg = cfg.get("output", {}) or {}
    write_weights_csvs = bool(out_cfg.get("write_weights_csvs", True))

    rng = np.random.default_rng(42)

    _apply_macro_optimizer_integration(cfg, cfg_path_obj)

    # Load inputs
    paths = _require_cfg_section(cfg, "paths")
    stocks = load_stocks_scores(_resolve_cfg_path(paths["stocks_scores_csv"], cfg, cfg_path_obj))
    sector_rot = load_sector_rotation(_resolve_cfg_path(paths["sector_rotation_csv"], cfg, cfg_path_obj))
    foreign = load_foreign_etfs(_resolve_cfg_path(paths["foreign_etfs_csv"], cfg, cfg_path_obj))
    foreign = _filter_stage12d_foreign_universe(foreign, cfg)
    company_raw = paths.get("ticker_company_csv", None)
    if company_raw is not None and str(company_raw).strip() != "":
        company_path = _resolve_cfg_path(company_raw, cfg, cfg_path_obj)
        company_map = load_ticker_company_map(company_path)
        if company_map:
            if "Company" not in stocks.columns:
                stocks["Company"] = ""
            stocks["Company"] = stocks["Company"].fillna("").astype(str).str.strip()
            stocks.loc[stocks["Company"] == "", "Company"] = stocks["Ticker"].map(
                lambda t: company_map.get(str(t).upper(), "")
            )

            if "Company" not in foreign.columns:
                foreign["Company"] = ""
            foreign["Company"] = foreign["Company"].fillna("").astype(str).str.strip()
            foreign.loc[foreign["Company"] == "", "Company"] = foreign["Ticker"].map(
                lambda t: company_map.get(str(t).upper(), "")
            )

    stocks = _apply_stocks_universe_filters(stocks, cfg)

    # Check if long_short portfolio is enabled (default True for backward compatibility)
    ls_cfg = cfg.get("optimization", {}).get("long_short", {}) or {}
    ls_enabled = bool(ls_cfg.get("enabled", True))

    # Select universes
    us_long_only = select_us_long_only(stocks, cfg)
    us_long_short = select_us_long_short(stocks, cfg) if ls_enabled else pd.DataFrame()
    foreign_sel = select_foreign_etfs(foreign, cfg)

    # Build risky asset tables
    assets_lo = build_assets_table(us_long_only, sector_rot, foreign_sel, cfg, portfolio_type="long_only")
    assets_ls = build_assets_table(us_long_short, sector_rot, foreign_sel, cfg, portfolio_type="long_short") if ls_enabled else pd.DataFrame()

    # Returns provider
    rc = _require_cfg_section(cfg, "returns")
    start_str, end_str = _get_returns_window(cfg, root_cfg)
    provider = _build_returns_provider(
        cfg=cfg,
        root_cfg=root_cfg,
        prices_by_ticker=prices_by_ticker,
        provider=provider,
        cfg_path=cfg_path_obj,
    )

    # Cash return (period)
    ppy = periods_per_year(str(rc.get("frequency", "W-FRI")))
    cash_ann = float(cfg.get("cash", {}).get("annual_yield", 0.0))
    cash_p = annual_to_period_rate(cash_ann, ppy)

    # Compute returns for each portfolio universe separately (so selection differs)
    def get_rets_for_assets(assets: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
        tickers = assets["Ticker"].tolist()
        try:
            rets = provider.get_returns(
                tickers=tickers,
                start=start_str,
                end=end_str,
                freq=str(rc.get("frequency", "W-FRI")),
                log_returns=bool(rc.get("log_returns", True)),
            )
        except Exception:
            if not _uses_precomputed_covariance(cfg):
                raise
            logger.warning(
                "Returns provider failed, but precomputed covariance is active; continuing with empty diagnostic returns.",
                exc_info=True,
            )
            rets = pd.DataFrame(columns=tickers)
        if _uses_precomputed_covariance(cfg):
            for t in tickers:
                if t not in rets.columns:
                    rets[t] = np.nan
            rets = rets.loc[:, tickers].copy()
            return assets.copy().reset_index(drop=True), rets

        rets = clean_returns_df(rets, cfg)
        # Align assets to returns columns (drop missing)
        common = [t for t in tickers if t in rets.columns]
        missing = sorted(set(tickers) - set(common))
        if missing:
            logger.warning("Dropping assets with missing returns: %s", missing)
        rets = rets[common].copy()
        # Filter assets too
        assets = assets[assets["Ticker"].isin(common)].copy()
        assets.reset_index(drop=True, inplace=True)
        return assets, rets

    assets_lo, rets_lo = get_rets_for_assets(assets_lo)
    rets_ls: pd.DataFrame | None = None
    if ls_enabled:
        assets_ls, rets_ls = get_rets_for_assets(assets_ls)

    results: Dict[str, OptResult] = {}

    # ---- Long-only ----
    assets_lo_f, rets_lo_f, cov_lo_f, mu_lo_f, w_lo, metrics_lo, turnover_extra_lo = optimize_prune_reoptimize(
        portfolio_name="LONG_ONLY",
        assets=assets_lo,
        rets=rets_lo,
        cfg=cfg,
        ppy=ppy,
        cash_p=cash_p,
        rng=rng,
        prev_weights=None,
        is_long_short=False,
    )
    low_lo, high_lo = compute_weight_bands(
        portfolio_name="LONG_ONLY",
        assets=assets_lo_f,
        rets=rets_lo_f,
        mu_risky=mu_lo_f,
        cov_scenarios=cov_lo_f,
        cfg=cfg,
        ppy=ppy,
        cash_period_return=cash_p,
        prev_weights=None,
        is_long_short=False,
        turnover_extra=turnover_extra_lo,
        allow_cash_budget_relaxation=bool(metrics_lo.get("cash_budget_relaxation_used", False)),
    )
    df_lo = assemble_output(assets_lo_f, w_lo, low_lo, high_lo, cfg)
    results["LONG_ONLY"] = OptResult("LONG_ONLY", df_lo, metrics_lo)
    if write_weights_csvs:
        _atomic_dataframe_csv(df_lo, os.path.join(out_dir, "weights_long_only.csv"))

    # ---- Long-short ----
    if ls_enabled:
        if rets_ls is None:
            raise RuntimeError("LONG_SHORT enabled but returns were not computed.")
        assets_ls_f, rets_ls_f, cov_ls_f, mu_ls_f, w_ls, metrics_ls, turnover_extra_ls = optimize_prune_reoptimize(
            portfolio_name="LONG_SHORT",
            assets=assets_ls,
            rets=rets_ls,
            cfg=cfg,
            ppy=ppy,
            cash_p=cash_p,
            rng=rng,
            prev_weights=None,
            is_long_short=True,
        )
        low_ls, high_ls = compute_weight_bands(
            portfolio_name="LONG_SHORT",
            assets=assets_ls_f,
            rets=rets_ls_f,
            mu_risky=mu_ls_f,
            cov_scenarios=cov_ls_f,
            cfg=cfg,
            ppy=ppy,
            cash_period_return=cash_p,
            prev_weights=None,
            is_long_short=True,
            turnover_extra=turnover_extra_ls,
            allow_cash_budget_relaxation=bool(metrics_ls.get("cash_budget_relaxation_used", False)),
        )
        df_ls = assemble_output(assets_ls_f, w_ls, low_ls, high_ls, cfg)
        results["LONG_SHORT"] = OptResult("LONG_SHORT", df_ls, metrics_ls)
        if write_weights_csvs:
            _atomic_dataframe_csv(df_ls, os.path.join(out_dir, "weights_long_short.csv"))
    else:
        logger.info("LONG_SHORT portfolio disabled in config (optimization.long_short.enabled=false).")

    # ---- Optional user portfolio ----
    up_cfg = cfg.get("user_portfolio", {}) or {}
    if bool(up_cfg.get("enabled", False)):
        user_portfolio_csv = paths.get("user_portfolio_csv")
        if user_portfolio_csv is None or str(user_portfolio_csv).strip() == "":
            raise ValueError(
                "user_portfolio.enabled=true but paths.user_portfolio_csv is not configured."
            )
        user_pf = load_user_portfolio(_resolve_cfg_path(str(user_portfolio_csv), cfg, cfg_path_obj))
        results["USER_PORTFOLIO"] = optimize_user_portfolio(
            user_pf=user_pf,
            stocks_all=stocks,
            sector_rot=sector_rot,
            foreign_sel=foreign_sel,
            provider=provider,
            cfg=cfg,
            start_str=start_str,
            end_str=end_str,
            ppy=ppy,
            cash_p=cash_p,
            out_dir=out_dir,
            rng=rng
        )

    # Save summary + weights into one CSV
    summary_rows: List[Dict[str, Any]] = []
    for name, res in results.items():
        row: Dict[str, Any] = {"RowType": "SUMMARY", "Portfolio": name}
        row.update(res.metrics)
        summary_rows.append(row)

    combined_frames = [pd.DataFrame(summary_rows)]
    for name, res in results.items():
        weights_df = res.weights.copy()
        weights_df.insert(0, "RowType", "WEIGHTS")
        weights_df.insert(1, "Portfolio", name)
        combined_frames.append(weights_df)

    combined = pd.concat(combined_frames, ignore_index=True, sort=False)
    _atomic_dataframe_csv(combined, os.path.join(out_dir, "optimization_results.csv"))

    return results


def run_end_to_end(config_path: str) -> Dict[str, OptResult]:
    cfg = load_yaml(config_path)
    return run_end_to_end_from_cfg(cfg, cfg_path=Path(config_path))


def assemble_output(
    assets: pd.DataFrame,
    w: pd.Series,
    low: pd.Series,
    high: pd.Series,
    cfg: Dict[str, Any]
) -> pd.DataFrame:
    cash_symbol = (cfg.get("universe", {}) or {}).get("cash_symbol", "CASH")
    rows = []

    # risky assets
    for i, (_, a) in enumerate(assets.iterrows()):
        t = str(a["Ticker"])
        w_val = w.get(t, 0.0)
        low_val = low.get(t, 0.0)
        high_val = high.get(t, 0.0)
        rows.append({
            "Ticker": t,
            "Company": a.get("Company", ""),
            "Sleeve": a.get("Sleeve", ""),
            "AssetType": a.get("AssetType", ""),
            "Rating": a.get("Rating", ""),
            "LS_Book": a.get("LS_Book", ""),
            "SectorName": a.get("SectorName", ""),
            "IndustryAggregateName": a.get("IndustryAggregateName", ""),
            "IndustryName": a.get("IndustryName", ""),
            "RegionGroup": a.get("RegionGroup", ""),
            "SignalScore": float(a.get("SignalScore", np.nan)) if "SignalScore" in a else np.nan,
            "NextEarningsDate": a.get("NextEarningsDate", ""),
            "EarningsDaysAhead": a.get("EarningsDaysAhead", ""),
            "EarningsDaysAheadAsOf": a.get("EarningsDaysAheadAsOf", ""),
            "EarningsFilterNote": a.get("EarningsFilterNote", ""),
            "Weight": float(w_val),
            "Low": float(low_val),
            "High": float(high_val),
        })

    # cash row
    rows.append({
        "Ticker": cash_symbol,
        "Company": "CASH",
        "Sleeve": "CASH",
        "AssetType": "CASH",
        "Rating": "CASH",
        "SectorName": "CASH",
        "IndustryAggregateName": "CASH",
        "IndustryName": "CASH",
        "RegionGroup": "CASH",
        "LS_Book": "",
        "SignalScore": np.nan,
        "NextEarningsDate": "",
        "EarningsDaysAhead": "",
        "EarningsDaysAheadAsOf": "",
        "EarningsFilterNote": "",
        "Weight": float(w.get(cash_symbol, 0.0)),
        "Low": float(low.get(cash_symbol, 0.0)),
        "High": float(high.get(cash_symbol, 0.0)),
    })

    out = pd.DataFrame(rows)
    out["AbsWeight"] = out["Weight"].abs()
    out = out.sort_values(["Sleeve", "AbsWeight"], ascending=[True, False]).drop(columns=["AbsWeight"])
    return out


def optimize_user_portfolio(
    user_pf: pd.DataFrame,
    stocks_all: pd.DataFrame,
    sector_rot: pd.DataFrame,
    foreign_sel: pd.DataFrame,
    provider: ReturnsDataProvider,
    cfg: Dict[str, Any],
    start_str: str,
    end_str: Optional[str],
    ppy: int,
    cash_p: float,
    out_dir: str,
    rng: np.random.Generator
) -> OptResult:
    """
    Optimize within a user-provided holdings universe (plus foreign ETFs + cash),
    optionally restricting to holdings only, with turnover penalty relative to current weights.
    """
    up_cfg = cfg.get("user_portfolio", {}) or {}
    restrict = bool(up_cfg.get("restrict_to_holdings", True))
    ucfg = _require_cfg_section(cfg, "universe")
    opt_cfg = _require_cfg_section(cfg, "optimization")

    holdings = user_pf["Ticker"].astype(str).str.upper().tolist()
    holdings_set = set(holdings)
    stocks_all_tickers = set(stocks_all["Ticker"].astype(str).str.upper().tolist())
    not_in_universe = sorted(holdings_set - stocks_all_tickers)
    if not_in_universe:
        logger.warning(
            "User portfolio tickers not in stocks_scores universe (excluded): %s",
            not_in_universe,
        )
    us_hold = stocks_all[stocks_all["Ticker"].isin(holdings)].copy()

    if restrict:
        us_universe = us_hold.copy()
    else:
        # Union of holdings + extra candidates from top-scoring stocks not already held
        extra_n = int(up_cfg.get("extra_candidates", 5))
        non_held = stocks_all[~stocks_all["Ticker"].isin(holdings)].copy()
        # Select top extra_n by FinalScore from allowed ratings
        allowed_ratings = {"Strong Buy", "Buy", "Hold"}
        non_held = non_held[non_held["Rating"].isin(allowed_ratings)]
        extras = non_held.sort_values("FinalScore", ascending=False).head(extra_n)
        us_universe = pd.concat([us_hold, extras], ignore_index=True).drop_duplicates(subset=["Ticker"])

    if us_universe.empty:
        raise ValueError("User portfolio tickers not found in stocks_scores universe.")

    us_universe["Sleeve"] = "US"
    assets_up = build_assets_table(us_universe, sector_rot, foreign_sel, cfg, portfolio_type="user_portfolio")

    # Previous weights vector
    prev = user_pf.set_index("Ticker")["Weight"].copy()
    # Add cash if missing
    cash_symbol = ucfg.get("cash_symbol", "CASH")
    if cash_symbol not in prev.index:
        prev[cash_symbol] = max(0.0, 1.0 - float(prev.sum()))

    # Returns
    tickers = assets_up["Ticker"].tolist()
    rc = _require_cfg_section(cfg, "returns")
    rets = provider.get_returns(
        tickers=tickers,
        start=start_str,
        end=end_str,
        freq=str(rc.get("frequency", "W-FRI")),
        log_returns=bool(rc.get("log_returns", True)),
    )
    rets = clean_returns_df(rets, cfg)
    common = [t for t in tickers if t in rets.columns]
    missing_returns = sorted(set(tickers) - set(common))
    dropped_holdings_no_rets = sorted(holdings_set.intersection(missing_returns))
    extra_from_dropped = 0.0
    if dropped_holdings_no_rets:
        extra_from_dropped = float(sum(abs(prev.get(t, 0.0)) for t in dropped_holdings_no_rets))
        logger.warning(
            "User portfolio holdings dropped (no return data): %s. "
            "Their prior weights counted in turnover_extra.",
            dropped_holdings_no_rets,
        )
    other_missing_returns = [t for t in missing_returns if t not in set(dropped_holdings_no_rets)]
    if other_missing_returns:
        logger.warning(
            "User portfolio optimization assets dropped (no return data): %s",
            other_missing_returns,
        )
    assets_up = assets_up[assets_up["Ticker"].isin(common)].copy().reset_index(drop=True)
    rets = rets[common].copy()

    # Use higher turnover penalty for user portfolio if configured
    cfg_up = dict(cfg)
    cfg_up["optimization"] = dict(opt_cfg)
    cfg_up["optimization"]["turnover_penalty"] = float(up_cfg.get("turnover_penalty", opt_cfg.get("turnover_penalty", 0.10)))

    # Option-1 pruning is supported for user portfolios as well (and keeps max_turnover honest).
    assets_up_f, rets_up_f, cov_up_f, mu_up_f, w_up, metrics_up, turnover_extra_up = optimize_prune_reoptimize(
        portfolio_name="USER_PORTFOLIO",
        assets=assets_up,
        rets=rets,
        cfg=cfg_up,
        ppy=ppy,
        cash_p=cash_p,
        rng=rng,
        prev_weights=prev,
        is_long_short=False,
        initial_turnover_extra=extra_from_dropped,
    )

    low_up, high_up = compute_weight_bands(
        portfolio_name="USER_PORTFOLIO",
        assets=assets_up_f,
        rets=rets_up_f,
        mu_risky=mu_up_f,
        cov_scenarios=cov_up_f,
        cfg=cfg_up,
        ppy=ppy,
        cash_period_return=cash_p,
        prev_weights=prev,
        is_long_short=False,
        turnover_extra=turnover_extra_up,
        allow_cash_budget_relaxation=bool(metrics_up.get("cash_budget_relaxation_used", False)),
    )

    df_up = assemble_output(assets_up_f, w_up, low_up, high_up, cfg)
    _atomic_dataframe_csv(df_up, os.path.join(out_dir, "weights_user_portfolio.csv"))

    return OptResult("USER_PORTFOLIO", df_up, metrics_up)


# --------------------------
# CLI
# --------------------------
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Path to config.yaml")
    return p.parse_args()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    args = parse_args()
    results = run_end_to_end(args.config)

    # Print a compact console summary
    for name, res in results.items():
        m = res.metrics
        logger.info(
            "%s | exp_return_ann=%.3f | vol_ann=%.3f | sharpe=%.3f",
            name, m.get("exp_return_ann", float("nan")), m.get("vol_ann", float("nan")), m.get("sharpe_ann", float("nan"))
        )
        top = res.weights.sort_values("Weight", ascending=False).head(10)[["Ticker", "Weight", "Low", "High", "Sleeve"]]
        logger.info("Top weights (%s):\n%s", name, top.to_string(index=False))


if __name__ == "__main__":
    main()
