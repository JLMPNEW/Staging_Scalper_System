#!/usr/bin/env python3
"""Stage 2 - validate the risk panel + covariance and seal the risk manifest."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import numpy as np  # noqa: E402

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists, read_csv, sha256_file, write_csv, write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import (  # noqa: E402
    finite_float,
    liquidity_panel_active,
    load_spread_snapshot,
)
from portfolio_layer.risk.panel import classify_coverage, coverage_stats, to_returns  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_risk_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate the Stage 2 risk panel.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


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
    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    validation_path = risk_dir / "validation" / "risk_panel_validation.csv"
    review_path = risk_dir / "data_quality_review.csv"
    manifest_path = risk_dir / "risk_manifest.json"
    try:
        fail_if_exists([validation_path, review_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    rc = cfg_get(config, "risk_panel", {})
    min_direct = int(rc.get("min_direct_history_days", 252))
    hard_floor = int(rc.get("hard_floor_history_days", 60))
    max_gap = float(rc.get("max_missing_day_fraction", 0.10))
    max_stale_days = int(rc.get("max_stale_price_trading_days", 0))
    benchmarks = [str(x).upper() for x in rc.get("benchmark_tickers", [])]
    market_required = set(benchmarks)
    market_required.update(str(x).upper() for x in rc.get("hedge_rotation_etfs", []))
    market_required.update(str(x).upper() for x in (rc.get("sector_etf_map", {}) or {}).values())
    market_required.add(str(rc.get("master_calendar_ticker", "SPY")).upper())

    prices = pd.read_csv(risk_dir / "prices_adjclose.csv", index_col=0)
    returns = pd.read_csv(risk_dir / "returns_panel.csv", index_col=0)
    coverage = {r["ticker"]: r for r in read_csv(risk_dir / "risk_coverage.csv")}
    clusters = {r["ticker"]: r["cluster_id"] for r in read_csv(risk_dir / "correlation_clusters.csv")}
    meta = json.loads((risk_dir / "covariance_meta.json").read_text(encoding="utf-8"))
    covariance = pd.read_csv(risk_dir / "covariance.csv", index_col=0)
    snapshot = json.loads((risk_dir / "price_snapshot.json").read_text(encoding="utf-8"))
    scores = read_csv(run_dir / "stocks_scores.csv")
    split_events_path = risk_dir / "split_events.csv"
    split_events_missing = not split_events_path.exists()
    split_event_rows = [] if split_events_missing else read_csv(split_events_path)
    splits_by_ticker: dict[str, list[dict[str, str]]] = {}
    for row in split_event_rows:
        splits_by_ticker.setdefault(str(row.get("ticker", "")).upper(), []).append(row)
    panel_end = str(prices.index[-1]) if not prices.empty else run_as_of

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. calendar / no future dates
    dates = list(returns.index)
    future = [d for d in dates if str(d) > run_as_of]
    monotonic = dates == sorted(dates)
    rec("calendar_no_future_dates", "PASS" if not future and monotonic else "FAIL",
        f"rows={len(dates)} last={dates[-1] if dates else 'none'}" if not future and monotonic
        else f"future={future[:3]} monotonic={monotonic}")

    # 1b. returns must be recomputable from prices without forward-filling missing bars
    expected_returns_input = prices.copy()
    expected_returns_input.index = pd.to_datetime(expected_returns_input.index)
    expected_returns = to_returns(expected_returns_input, str(rc.get("covariance_frequency", "daily")))
    expected_returns.index = [d.date().isoformat() for d in expected_returns.index]
    expected_returns = expected_returns.reindex(index=returns.index, columns=returns.columns)
    fabricated = []
    mismatch = []
    for ticker in returns.columns:
        actual = returns[ticker]
        expected = expected_returns[ticker]
        bad_fill = actual.notna() & expected.isna()
        if bad_fill.any():
            fabricated.append(f"{ticker}:{list(actual.index[bad_fill])[:3]}")
        both = actual.notna() & expected.notna()
        diff = (actual[both] - expected[both]).abs()
        if (diff > 1e-12).any():
            mismatch.append(f"{ticker}:max_diff={float(diff.max())}")
    rec("returns_match_no_fill_prices", "PASS" if not fabricated and not mismatch else "FAIL",
        "returns recompute from prices with fill_method=None" if not fabricated and not mismatch else (
            f"fabricated={fabricated[:5]} mismatch={mismatch[:5]}"
        ))

    # 2. Split-aligned data-quality scan from the sealed split_events.csv artifact. Real catalyst moves
    #    are kept; split-aligned suspects hard-fail until an explicit quarantine/override path exists.
    threshold = float(rc.get("split_jump_threshold", 0.8))
    window = int(rc.get("split_review_window_days", 7))
    artifact_abs_tol = float(rc.get("split_artifact_abs_tolerance", 0.15))
    artifact_rel_tol = float(rc.get("split_artifact_rel_tolerance", 0.25))

    def _days_between(a: str, b: str) -> int:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)

    def _split_implied_return(row: dict[str, str]) -> float | None:
        try:
            numerator = float(row.get("numerator") or 0.0)
            denominator = float(row.get("denominator") or 0.0)
        except ValueError:
            return None
        if numerator <= 0 or denominator <= 0:
            return None
        return denominator / numerator - 1.0

    def _looks_like_split_artifact(observed_return: float, expected_return: float | None) -> bool:
        if expected_return is None:
            return False
        diff = abs(observed_return - expected_return)
        rel = diff / max(abs(expected_return), 1e-12)
        return diff <= artifact_abs_tol or rel <= artifact_rel_tol

    review_rows: list[dict] = []
    suspects: list[str] = []
    for t in returns.columns:
        s = returns[t].dropna()
        big = {str(d): float(v) for d, v in s.items() if abs(float(v)) > threshold}
        if not big:
            continue
        splits = [row for row in splits_by_ticker.get(str(t).upper(), []) if row.get("split_date")]
        for d, v in big.items():
            nearest = min(splits, key=lambda row: _days_between(str(row["split_date"]), d), default={})
            near = str(nearest.get("split_date") or "")
            aligned = bool(near and _days_between(near, d) <= window)
            expected = _split_implied_return(nearest) if aligned else None
            artifact_like = aligned and _looks_like_split_artifact(v, expected)
            classification = (
                "split_events_missing" if split_events_missing
                else "suspect_unadjusted_split" if artifact_like
                else "split_near_real_move" if aligned
                else "real_move"
            )
            review_rows.append({
                "ticker": t, "date": d, "signed_return": round(v, 4), "abs_return": round(abs(v), 4),
                "nearest_split_date": near,
                "split_ratio": str(nearest.get("split_ratio") or ""),
                "expected_unadjusted_split_return": "" if expected is None else round(expected, 4),
                "split_aligned": int(aligned),
                "split_artifact_like": int(bool(artifact_like)),
                "classification": classification,
            })
            if artifact_like:
                suspects.append(f"{t}@{d}~split:{near}")
    write_csv(
        review_path,
        [
            "ticker", "date", "signed_return", "abs_return", "nearest_split_date", "split_ratio",
            "expected_unadjusted_split_return", "split_aligned", "split_artifact_like", "classification",
        ],
        sorted(review_rows, key=lambda r: -abs(float(r["abs_return"]))),
    )
    flagged = bool(suspects or split_events_missing)
    rec("split_aligned_data_quality", "PASS" if not flagged else "FAIL",
        f"{len(review_rows)} large moves reviewed; {len(suspects)} split-artifact suspects; "
        f"split_events_file={'missing' if split_events_missing else 'present'}"
        + (f"; suspects={suspects[:5]}" if suspects else " (large moves are real catalyst events, kept)"))

    # 3. coverage completeness: every eligible scored name has a coverage row
    eligible = [r["ticker"] for r in scores if str(r.get("investable_eligible", "")).strip() == "1"]
    missing_cov = [t for t in eligible if t not in coverage]
    rec("coverage_complete", "PASS" if not missing_cov else "FAIL",
        "all eligible names covered" if not missing_cov else f"{len(missing_cov)} missing: {missing_cov[:10]}")

    # 3b. coverage fields must match the raw price panel, including right-edge missing bars.
    bad_cov_fields = []
    for t, r in coverage.items():
        stats = coverage_stats(prices, t, panel_end)
        checks_to_compare = (
            ("observation_count", int(float(r.get("observation_count") or 0)), stats["observation_count"]),
            ("missing_day_count", int(float(r.get("missing_day_count") or 0)), stats["missing_day_count"]),
            (
                "right_edge_missing_day_count",
                int(float(r.get("right_edge_missing_day_count") or 0)),
                stats["right_edge_missing_day_count"],
            ),
            ("start_date", str(r.get("start_date") or ""), stats["start_date"]),
            ("end_date", str(r.get("end_date") or ""), stats["end_date"]),
        )
        for field, recorded, expected in checks_to_compare:
            if recorded != expected:
                bad_cov_fields.append(f"{t}:{field}={recorded}!={expected}")
                break
    rec("coverage_matches_price_panel", "PASS" if not bad_cov_fields else "FAIL",
        "coverage fields match prices through panel end" if not bad_cov_fields else (
            f"{len(bad_cov_fields)} mismatch: {bad_cov_fields[:10]}"
        ))

    # 4. thin-history routing matches thresholds
    bad_route = []
    for t, r in coverage.items():
        stats = coverage_stats(prices, t, panel_end)
        expect, _, _ = classify_coverage(
            stats,
            min_direct=min_direct,
            hard_floor=hard_floor,
            max_gap_frac=max_gap,
            max_stale_days=max_stale_days,
        )
        if r["risk_status"] != expect:
            bad_route.append(f"{t}:{r['risk_status']}!={expect}")
    rec("thin_history_routing", "PASS" if not bad_route else "FAIL",
        "risk_status matches thresholds" if not bad_route else f"{len(bad_route)}: {bad_route[:10]}")

    # 5. benchmark/hedge/rotation ETF coverage (present + direct + full price history)
    calendar_days = int(snapshot.get("calendar_days") or prices.shape[0])
    bad_market = []
    for ticker in sorted(market_required):
        row = coverage.get(ticker)
        if not row:
            bad_market.append(f"{ticker}:missing")
            continue
        stats = coverage_stats(prices, ticker, panel_end)
        obs = int(stats["observation_count"])
        right_edge_missing = int(stats["right_edge_missing_day_count"])
        if row["risk_status"] != "direct" or obs < calendar_days or right_edge_missing:
            bad_market.append(
                f"{ticker}:{row['risk_status']}:obs={obs}/{calendar_days}:tail_missing={right_edge_missing}"
            )
    rec("market_instrument_full_coverage", "PASS" if not bad_market else "FAIL",
        "benchmarks/hedges/rotation ETFs have full direct history" if not bad_market else (
            f"missing/non-full: {bad_market}"
        ))

    # 6. FX normalization (US universe / Yahoo USD) — informational
    rec("fx_usd", "PASS", "universe is US-listed USD via Yahoo adjusted close")

    # 7. covariance PSD + condition number, recomputed from the artifact Stage 3 consumes.
    cov_square = covariance.shape[0] == covariance.shape[1]
    cov_labels_match = list(map(str, covariance.index)) == list(map(str, covariance.columns))
    cov_numeric = covariance.apply(pd.to_numeric, errors="coerce")
    cov_values = cov_numeric.to_numpy(dtype=float)
    cov_finite = bool(np.isfinite(cov_values).all()) if cov_values.size else False
    cov_symmetric = False
    cov_min_eig: float | None = None
    cov_condition: float | None = None
    if cov_square and cov_finite:
        cov_symmetric = bool(np.allclose(cov_values, cov_values.T, rtol=0.0, atol=1e-10))
        try:
            sym_cov = 0.5 * (cov_values + cov_values.T)
            eigvals = np.linalg.eigvalsh(sym_cov)
            cov_min_eig = float(eigvals.min())
            cov_condition = float(np.linalg.cond(sym_cov))
        except np.linalg.LinAlgError:
            cov_min_eig = None
            cov_condition = None

    def _meta_float(key: str) -> float | None:
        try:
            value = float(meta.get(key))
        except (TypeError, ValueError):
            return None
        return value if np.isfinite(value) else None

    def _close(a: float | None, b: float | None, *, rel: float = 1e-6, abs_tol: float = 1e-10) -> bool:
        if a is None or b is None:
            return False
        return abs(a - b) <= max(abs_tol, rel * max(abs(a), abs(b), 1.0))

    meta_min_eig = _meta_float("psd_min_eig")
    meta_condition = _meta_float("condition_number")
    max_condition = _meta_float("max_condition_number") or float(rc.get("max_condition_number", 1e6))
    matrix_psd = (
        cov_square and cov_labels_match and cov_finite and cov_symmetric
        and cov_min_eig is not None and cov_min_eig > 0
    )
    meta_psd_matches = _close(cov_min_eig, meta_min_eig)
    matrix_condition_ok = cov_condition is not None and cov_condition <= max_condition
    meta_condition_matches = _close(cov_condition, meta_condition, rel=1e-5, abs_tol=1e-6)
    meta_condition_flag_matches = bool(meta.get("condition_ok")) == bool(matrix_condition_ok)
    rec("covariance_psd", "PASS" if matrix_psd and meta_psd_matches else "FAIL",
        f"matrix_min_eig={cov_min_eig} meta_min_eig={meta_min_eig} "
        f"square={cov_square} labels_match={cov_labels_match} finite={cov_finite} symmetric={cov_symmetric}")
    rec("covariance_condition_number",
        "PASS" if matrix_condition_ok and meta_condition_matches and meta_condition_flag_matches else "FAIL",
        f"matrix_cond={cov_condition} meta_cond={meta_condition} max={max_condition} "
        f"meta_flag_matches={meta_condition_flag_matches}")
    rec("covariance_units_annualized", "PASS" if meta.get("covariance_units") == "annualized" else "FAIL",
        f"covariance_units={meta.get('covariance_units')}")

    # 8. clustering sanity: known-correlated control pair lands in one cluster
    control = ("SMH", "SOXX")
    if all(c in clusters for c in control):
        same = clusters[control[0]] == clusters[control[1]]
        rec("clustering_sanity", "PASS" if same else "FAIL",
            f"{control} cluster {clusters[control[0]]}/{clusters[control[1]]}")
    else:
        rec("clustering_sanity", "WARN", f"control pair {control} not both present")

    # 9. snapshot reproducibility: recorded hashes match files on disk
    bad_hash = []
    for fname, info in (snapshot.get("files") or {}).items():
        fp = risk_dir / fname
        if not fp.exists() or sha256_file(fp) != info.get("sha256"):
            bad_hash.append(fname)
    rec("price_snapshot_reproducible", "PASS" if not bad_hash else "FAIL",
        "snapshot hashes match panel files" if not bad_hash else f"mismatch: {bad_hash}")

    bad_cov_hash = []
    for fname, info in (meta.get("files") or {}).items():
        fp = risk_dir / fname
        if not fp.exists() or sha256_file(fp) != info.get("sha256"):
            bad_cov_hash.append(fname)
    rec("covariance_artifacts_reproducible", "PASS" if not bad_cov_hash else "FAIL",
        "covariance meta hashes match matrix artifacts" if not bad_cov_hash else f"mismatch: {bad_cov_hash}")

    # 10. Optional intraday liquidity panel. Disabled is the default and does not require IB artifacts.
    try:
        liquidity_enabled = liquidity_panel_active(config)
        liquidity_mode_error = ""
    except ValueError as exc:
        liquidity_enabled = False
        liquidity_mode_error = str(exc)
    spread_samples_path = risk_dir / "ib_spread_samples.csv"
    spread_snapshot_path = risk_dir / "spread_snapshot.csv"
    spread_meta_path = risk_dir / "spread_snapshot_meta.json"
    liquidity_audit_path = risk_dir / "liquidity_audit.csv"
    liquidity_sector_path = risk_dir / "liquidity_audit_by_sector.csv"
    liquidity_summary_path = risk_dir / "liquidity_audit_summary.json"
    if liquidity_mode_error:
        rec("liquidity_panel_mode", "FAIL", liquidity_mode_error)
    elif not liquidity_enabled:
        rec("liquidity_panel_mode", "PASS", "enhanced spread panel inactive; Stage 4 uses config/default spread")
    else:
        liquidity_bad = []
        spread_rows = {}
        spread_meta = {}
        if not spread_samples_path.exists():
            liquidity_bad.append("missing_ib_spread_samples.csv")
        if not spread_snapshot_path.exists():
            liquidity_bad.append("missing_spread_snapshot.csv")
        else:
            spread_rows = load_spread_snapshot(spread_snapshot_path)
        if not spread_meta_path.exists():
            liquidity_bad.append("missing_spread_snapshot_meta.json")
        else:
            spread_meta = json.loads(spread_meta_path.read_text(encoding="utf-8"))
            for fname, info in (spread_meta.get("files") or {}).items():
                fp = risk_dir / fname
                if not fp.exists() or sha256_file(fp) != info.get("sha256"):
                    liquidity_bad.append(f"hash_mismatch:{fname}")
        fallback = sum(1 for row in spread_rows.values() if str(row.get("spread_status", "")) == "fallback")
        failed = sum(1 for row in spread_rows.values() if str(row.get("spread_status", "")) == "failed")
        max_fallback = finite_float(cfg_get(config, "liquidity_panel.max_universe_fallback_fraction", 0.10),
                                    name="liquidity_panel.max_universe_fallback_fraction")
        max_stale = int(finite_float(cfg_get(config, "liquidity_panel.max_stale_liquidity_days", 5),
                                     name="liquidity_panel.max_stale_liquidity_days"))
        fallback_fraction = fallback / len(spread_rows) if spread_rows else 0.0
        if fallback_fraction > max_fallback + 1e-12:
            liquidity_bad.append(f"fallback_fraction={fallback_fraction:.4f}>{max_fallback}")
        if failed:
            liquidity_bad.append(f"failed_spread_rows={failed}")
        stale_bad = []
        for ticker, row in spread_rows.items():
            status = str(row.get("spread_status", "")).strip()
            if status not in {"ok", "ok_latest_available"}:
                continue
            try:
                age = int(finite_float(row.get("latest_sample_age_days", 0),
                                       name=f"spread_snapshot:{ticker}.latest_sample_age_days"))
            except ValueError:
                stale_bad.append(f"{ticker}:missing_age")
                continue
            if age < 0 or age > max_stale:
                stale_bad.append(f"{ticker}:age={age}>{max_stale}")
        if stale_bad:
            liquidity_bad.append(f"stale_liquidity_rows={stale_bad[:10]}")
        rec("liquidity_panel_mode", "PASS" if not liquidity_bad else "FAIL",
            f"enabled snapshot_rows={len(spread_rows)} fallback={fallback} failed={failed}"
            if not liquidity_bad else f"{liquidity_bad[:10]}")

        audit_bad = []
        audit_summary = {}
        if not liquidity_audit_path.exists():
            audit_bad.append("missing_liquidity_audit.csv")
        if not liquidity_sector_path.exists():
            audit_bad.append("missing_liquidity_audit_by_sector.csv")
        if not liquidity_summary_path.exists():
            audit_bad.append("missing_liquidity_audit_summary.json")
        else:
            audit_summary = json.loads(liquidity_summary_path.read_text(encoding="utf-8"))
            for fname, info in (audit_summary.get("files") or {}).items():
                fp = risk_dir / fname
                if not fp.exists() or sha256_file(fp) != info.get("sha256"):
                    audit_bad.append(f"hash_mismatch:{fname}")
            if audit_summary.get("acceptance") != "PASS":
                audit_bad.append(f"audit_acceptance={audit_summary.get('acceptance')}")
        rec("liquidity_audit_reproducible", "PASS" if not audit_bad else "FAIL",
            "audit artifacts present and hashes match" if not audit_bad else f"{audit_bad[:10]}")
        for check in audit_summary.get("checks", []) if isinstance(audit_summary.get("checks"), list) else []:
            rec(
                f"liquidity_audit_{check.get('check')}",
                str(check.get("status", "FAIL")),
                str(check.get("detail", "")),
            )

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)

    hard = [c for c in checks if c["status"] not in ("WARN",)]
    passed = all(c["status"] == "PASS" for c in hard)
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage2_risk_panel",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "universe_size": snapshot.get("universe_size"),
        "covariance": {k: meta.get(k) for k in ("method_used", "n_names", "n_direct", "n_shrunk",
                                                "condition_number", "psd_min_eig", "n_clusters",
                                                "covariance_units", "n_return_outliers_capped")},
        "coverage_counts": pd.Series([r["risk_status"] for r in coverage.values()]).value_counts().to_dict(),
        "price_snapshot": {k: snapshot.get(k) for k in ("provider", "adjustment_policy", "fetch_timestamp",
                                                        "lookback_trading_days", "calendar_days", "fetched_ok",
                                                        "fetch_failed", "ticker_aliases_applied")},
        "files": {
            name: {"sha256": sha256_file(risk_dir / name)}
            for name in (
                "prices_adjclose.csv", "returns_panel.csv", "fetch_results.csv", "price_snapshot.json",
                "split_events.csv", "data_quality_review.csv",
                "ib_spread_samples.csv", "spread_snapshot.csv", "spread_snapshot_meta.json",
                "liquidity_audit.csv", "liquidity_audit_by_sector.csv", "liquidity_audit_summary.json",
                "risk_coverage.csv", "covariance.csv", "covariance_period.csv", "covariance_meta.json",
                "return_outliers.csv", "correlation_clusters.csv", "validation/risk_panel_validation.csv",
            )
            if (risk_dir / name).exists()
            and (
                liquidity_enabled
                or name not in {
                    "ib_spread_samples.csv",
                    "spread_snapshot.csv",
                    "spread_snapshot_meta.json",
                    "liquidity_audit.csv",
                    "liquidity_audit_by_sector.csv",
                    "liquidity_audit_summary.json",
                }
            )
        },
        "liquidity_panel": {
            "panel_active": liquidity_enabled,
            "enhanced_intraday_enabled": cfg_get(config, "liquidity_panel.enhanced_intraday_enabled", False),
            "spread_source": cfg_get(config, "transaction_costs.spread_source", "auto"),
            "provider": cfg_get(config, "liquidity_panel.provider", "ibkr_historical_bid_ask"),
            "max_universe_fallback_fraction": cfg_get(config, "liquidity_panel.max_universe_fallback_fraction", 0.10),
            "max_fallback_fraction": cfg_get(config, "liquidity_panel.max_fallback_fraction", 0.10),
            "max_stale_liquidity_days": cfg_get(config, "liquidity_panel.max_stale_liquidity_days", 5),
        },
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 2 ACCEPTANCE: PASS (as_of=%s, %s names) -> %s",
                    run_as_of, meta.get("n_names"), manifest_path)
        return 0
    LOGGER.error("STAGE 2 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
