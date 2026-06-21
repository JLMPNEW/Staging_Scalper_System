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

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists, read_csv, sha256_file, write_csv, write_manifest,
)
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.panel import to_returns  # noqa: E402
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
    snapshot = json.loads((risk_dir / "price_snapshot.json").read_text(encoding="utf-8"))
    scores = read_csv(run_dir / "stocks_scores.csv")
    split_events_path = risk_dir / "split_events.csv"
    split_events_missing = not split_events_path.exists()
    split_event_rows = [] if split_events_missing else read_csv(split_events_path)
    splits_by_ticker: dict[str, list[dict[str, str]]] = {}
    for row in split_event_rows:
        splits_by_ticker.setdefault(str(row.get("ticker", "")).upper(), []).append(row)
    panel_end = str(prices.index[-1]) if not prices.empty else run_as_of

    def price_coverage_stats(ticker: str) -> dict[str, str | int | float]:
        if ticker not in prices.columns:
            return {
                "observation_count": 0,
                "missing_day_count": prices.shape[0],
                "right_edge_missing_day_count": prices.shape[0],
                "missing_day_fraction": 1.0,
                "start_date": "",
                "end_date": "",
            }
        col = prices[ticker]
        obs = int(col.notna().sum())
        if obs == 0:
            return {
                "observation_count": 0,
                "missing_day_count": prices.shape[0],
                "right_edge_missing_day_count": prices.shape[0],
                "missing_day_fraction": 1.0,
                "start_date": "",
                "end_date": "",
            }
        present = col.dropna()
        first, last = str(present.index[0]), str(present.index[-1])
        span = prices.loc[first:panel_end].shape[0]
        missing = max(0, span - obs)
        right_edge_missing = max(0, prices.loc[last:panel_end].shape[0] - 1)
        return {
            "observation_count": obs,
            "missing_day_count": missing,
            "right_edge_missing_day_count": right_edge_missing,
            "missing_day_fraction": round(missing / span, 4) if span else 0.0,
            "start_date": first,
            "end_date": last,
        }

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
        stats = price_coverage_stats(t)
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
        stats = price_coverage_stats(t)
        obs = int(stats["observation_count"])
        gap = float(stats["missing_day_fraction"])
        right_edge_missing = int(stats["right_edge_missing_day_count"])
        if obs == 0 or obs < hard_floor:
            expect = "excluded"
        elif right_edge_missing > max_stale_days:
            expect = "excluded"
        elif obs < min_direct or gap > max_gap:
            expect = "shrunk"
        else:
            expect = "direct"
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
        stats = price_coverage_stats(ticker)
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

    # 7. covariance PSD + condition number
    rec("covariance_psd", "PASS" if float(meta.get("psd_min_eig", -1)) > 0 else "FAIL",
        f"min_eig={meta.get('psd_min_eig')}")
    rec("covariance_condition_number", "PASS" if meta.get("condition_ok") else "FAIL",
        f"cond={round(float(meta.get('condition_number', 0)), 1)} max={meta.get('max_condition_number')}")
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
                "risk_coverage.csv", "covariance.csv", "covariance_period.csv", "covariance_meta.json",
                "return_outliers.csv", "correlation_clusters.csv", "validation/risk_panel_validation.csv",
            )
            if (risk_dir / name).exists()
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
