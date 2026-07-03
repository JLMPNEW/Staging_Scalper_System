#!/usr/bin/env python3
"""Stage 2 - covariance model over risk-eligible names.

Direct names (full history) → Ledoit-Wolf/OAS shrinkage on the complete-case block. Shrunk names
(partial history) → single-index augmentation against their sector-ETF target. Excluded names are not
in the matrix. Output is PSD-fixed; condition number reported. Hierarchical clusters emitted for the
clustering-sanity gate.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path
from typing import cast


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.cluster.hierarchy import fcluster, linkage  # noqa: E402
from scipy.spatial.distance import squareform  # noqa: E402
from sklearn.covariance import OAS, LedoitWolf  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.covariance_utils import safe_cond, stabilize_covariance, symmetrize  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_covariance_model")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
ANNUALIZATION = {"daily": 252, "weekly": 52}


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build the Stage 2 covariance model.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def unlink_artifacts(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


def _series_col(df: pd.DataFrame, col: str) -> pd.Series:
    values = df[col]
    if isinstance(values, pd.DataFrame):
        values = values.iloc[:, 0]
    return cast(pd.Series, pd.to_numeric(values, errors="coerce"))


def _frame_cols(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    return cast(pd.DataFrame, df.loc[:, cols])


def _pairwise_linear(returns_df: "pd.DataFrame", intensity: float) -> tuple[np.ndarray, str]:
    """Pairwise sample covariance (full overlap per pair) shrunk linearly toward its diagonal."""
    sample = returns_df.cov().to_numpy(dtype=float)
    # Any pair with no overlap -> 0 covariance; diagonal must stay positive.
    sample = np.nan_to_num(sample, nan=0.0)
    diag = np.diag(np.diag(sample))
    shrunk = (1.0 - intensity) * sample + intensity * diag
    return shrunk, f"pairwise_linear(intensity={intensity})"


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    run_as_of = args.as_of or latest_run_with(runs_root, "manifest.json")
    if not run_as_of:
        LOGGER.error("No run found under %s", runs_root)
        return 1
    risk_dir = runs_root / run_as_of / "risk"
    returns_path = risk_dir / "returns_panel.csv"
    coverage_path = risk_dir / "risk_coverage.csv"
    if not (returns_path.exists() and coverage_path.exists()):
        LOGGER.error("Need returns_panel.csv and risk_coverage.csv; run 05 and 06 first")
        return 1
    cov_path = risk_dir / "covariance.csv"
    period_cov_path = risk_dir / "covariance_period.csv"
    clusters_path = risk_dir / "correlation_clusters.csv"
    meta_path = risk_dir / "covariance_meta.json"
    outliers_path = risk_dir / "return_outliers.csv"
    if args.force:
        unlink_artifacts([
            risk_dir / "data_quality_review.csv",
            risk_dir / "validation" / "risk_panel_validation.csv",
            risk_dir / "risk_manifest.json",
        ])
    try:
        fail_if_exists([cov_path, period_cov_path, clusters_path, meta_path, outliers_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    frequency = str(cfg_get(config, "risk_panel.covariance_frequency", "daily"))
    method = str(cfg_get(config, "risk_panel.shrinkage_method", "ledoit_wolf"))
    max_cond = float(cfg_get(config, "risk_panel.max_condition_number", 1e6))
    fallback_etf = str(cfg_get(config, "risk_panel.master_calendar_ticker", "SPY")).upper()
    annualization = ANNUALIZATION.get(frequency, 252)
    max_abs_return = float(cfg_get(config, "risk_panel.max_abs_return_for_covariance", 0.8))

    returns = pd.read_csv(returns_path, index_col=0)
    returns_for_cov = returns.copy()
    outlier_rows: list[dict] = []
    if max_abs_return > 0:
        mask = returns_for_cov.abs() > max_abs_return
        for day, ticker in zip(*np.where(mask.values), strict=False):
            raw = float(returns_for_cov.iat[day, ticker])
            capped = float(np.sign(raw) * max_abs_return)
            outlier_rows.append({
                "date": str(returns_for_cov.index[day]),
                "ticker": str(returns_for_cov.columns[ticker]),
                "raw_return": raw,
                "capped_return": capped,
            })
            returns_for_cov.iat[day, ticker] = capped
    write_csv(
        outliers_path,
        ["date", "ticker", "raw_return", "capped_return"],
        sorted(outlier_rows, key=lambda r: (str(r["ticker"]), str(r["date"]))),
    )
    coverage = read_csv(coverage_path)
    direct: list[str] = [
        str(r["ticker"])
        for r in coverage
        if r["risk_status"] == "direct" and r["ticker"] in returns_for_cov.columns
    ]
    shrunk: list[str] = [
        str(r["ticker"])
        for r in coverage
        if r["risk_status"] == "shrunk" and r["ticker"] in returns_for_cov.columns
    ]
    target_by = {str(r["ticker"]): str(r["shrinkage_target"] or fallback_etf).upper() for r in coverage}
    if not direct:
        LOGGER.error("No direct-history names to anchor the covariance estimate")
        return 1

    # Direct block estimate. Default `pairwise_linear` uses each pair's full overlap (ragged-panel safe)
    # then shrinks linearly toward the diagonal — this preserves true correlations. Complete-case LW/OAS
    # is offered but is unsafe when rows << names (it crushes correlations toward zero), so it is only
    # used on the fully-complete subset.
    intensity = float(cfg_get(config, "risk_panel.shrinkage_intensity", 0.10))
    complete_case_rows = 0
    if method in ("ledoit_wolf", "oas"):
        R = _frame_cols(returns_for_cov, direct).dropna(how="any")
        complete_case_rows = len(R)
        if len(R) >= len(direct):
            cov_d = (LedoitWolf() if method == "ledoit_wolf" else OAS()).fit(R.to_numpy(dtype=float)).covariance_
            used_method = f"{method}_complete_case(rows={len(R)})"
        else:
            LOGGER.warning("complete-case rows %d < names %d; falling back to pairwise_linear", len(R), len(direct))
            cov_d, used_method = _pairwise_linear(_frame_cols(returns_for_cov, direct), intensity)
    else:
        cov_d, used_method = _pairwise_linear(_frame_cols(returns_for_cov, direct), intensity)
    LOGGER.info("Direct block: %d names via %s", len(direct), used_method)
    pos = {name: i for i, name in enumerate(direct)}

    names = direct + shrunk
    n = len(names)
    idx = {name: i for i, name in enumerate(names)}
    C = np.zeros((n, n))
    C[np.ix_([idx[d] for d in direct], [idx[d] for d in direct])] = cov_d

    # Single-index augmentation for shrunk names.
    betas: dict[str, tuple[str, float, float]] = {}
    for s in shrunk:
        etf = target_by.get(s, fallback_etf)
        if etf not in pos:
            etf = fallback_etf
        if etf not in pos:
            continue
        pair = _frame_cols(returns_for_cov, [s, etf]).dropna()
        if len(pair) < 2:
            continue
        x = _series_col(pair, etf).to_numpy(dtype=float)
        y = _series_col(pair, s).to_numpy(dtype=float)
        var_e = float(np.var(x, ddof=1))
        beta = float(np.cov(x, y, ddof=1)[0, 1] / var_e) if var_e > 0 else 0.0
        idio = float(np.var(y - beta * x, ddof=1))
        betas[s] = (etf, beta, idio)
    for s in shrunk:
        if s not in betas:
            # no usable overlap: pure idiosyncratic from own returns
            v = float(np.var(_series_col(returns_for_cov, s).dropna().to_numpy(dtype=float), ddof=1))
            C[idx[s], idx[s]] = max(v, 1e-10)
            continue
        etf_s, beta_s, idio_s = betas[s]
        ei = pos[etf_s]
        for j in direct:
            cov_sj = beta_s * cov_d[ei, pos[j]]
            C[idx[s], idx[j]] = C[idx[j], idx[s]] = cov_sj
        C[idx[s], idx[s]] = beta_s * beta_s * cov_d[ei, ei] + idio_s
    for a in range(len(shrunk)):
        for b in range(a + 1, len(shrunk)):
            s1, s2 = shrunk[a], shrunk[b]
            if s1 in betas and s2 in betas:
                e1, b1, _ = betas[s1]
                e2, b2, _ = betas[s2]
                val = b1 * b2 * cov_d[pos[e1], pos[e2]]
                C[idx[s1], idx[s2]] = C[idx[s2], idx[s1]] = val

    # Stabilize with the shared tier1-derived utility (one risk-math implementation across Stage 2/3).
    eig_floor = max(1e-12, 1e-10 * float(np.mean(np.diag(C))))
    n_clipped = int((np.linalg.eigvalsh(symmetrize(C)) < eig_floor).sum())
    C_period_fixed = stabilize_covariance(C, eig_floor=eig_floor, max_cond=max_cond, name="stage2_risk")
    C_fixed = C_period_fixed * float(annualization)
    eigvals = np.linalg.eigvalsh(C_fixed)
    min_eig = float(eigvals.min())
    condition = safe_cond(C_fixed)

    index_labels = pd.Index(names, name="ticker")
    column_labels = pd.Index(names)
    period_cov_df = pd.DataFrame(C_period_fixed, index=index_labels, columns=column_labels)
    period_cov_df.to_csv(period_cov_path, lineterminator="\n")

    cov_df = pd.DataFrame(C_fixed, index=index_labels, columns=column_labels)
    cov_df.to_csv(cov_path, lineterminator="\n")

    # Hierarchical clusters from correlation distance (clustering-sanity gate).
    std = np.sqrt(np.diag(C_fixed))
    denom = np.outer(std, std)
    corr = np.divide(C_fixed, denom, out=np.zeros_like(C_fixed), where=denom > 0)
    dist = np.clip(1.0 - corr, 0.0, 2.0)
    np.fill_diagonal(dist, 0.0)
    cluster_rows = []
    cluster_threshold = float(cfg_get(config, "risk_panel.cluster_distance_threshold", 0.5))
    if n >= 3:
        Z = linkage(squareform(dist, checks=False), method="average")
        labels = fcluster(Z, t=cluster_threshold, criterion="distance")
        cluster_rows = [{"ticker": names[i], "cluster_id": int(labels[i])} for i in range(n)]
    write_csv(clusters_path, ["ticker", "cluster_id"], sorted(cluster_rows, key=lambda r: r["ticker"]))

    meta = {
        "run_as_of": run_as_of,
        "method": method,
        "method_used": used_method,
        "shrinkage_intensity": intensity,
        "max_abs_return_for_covariance": max_abs_return,
        "n_return_outliers_capped": len(outlier_rows),
        "frequency": frequency,
        "annualization_factor": annualization,
        "covariance_units": "annualized",
        "n_names": n,
        "n_direct": len(direct),
        "n_shrunk": len(shrunk),
        "complete_case_rows": int(complete_case_rows),
        "condition_number": condition,
        "max_condition_number": max_cond,
        "condition_ok": condition <= max_cond,
        "psd_min_eig": min_eig,
        "psd_eigs_clipped": n_clipped,
        "n_clusters": len({r["cluster_id"] for r in cluster_rows}) if cluster_rows else 0,
        "files": {
            "covariance.csv": {"sha256": sha256_file(cov_path), "rows": n},
            "covariance_period.csv": {"sha256": sha256_file(period_cov_path), "rows": n},
            "return_outliers.csv": {"sha256": sha256_file(outliers_path), "rows": len(outlier_rows)},
        },
    }
    write_manifest(meta_path, meta)
    LOGGER.info("Covariance %dx%d (direct=%d shrunk=%d) cond=%.1f clipped=%d clusters=%d -> %s",
                n, n, len(direct), len(shrunk), condition, n_clipped, meta["n_clusters"], cov_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
