#!/usr/bin/env python3
"""Stage 2.5 - audit the optional IBKR intraday liquidity panel.

This is a data-quality/reporting step over `spread_snapshot.csv`; it does not
fetch broker data. It enriches spreads with scores, risk coverage, target
weights, and trade notionals so Stage 4 cost changes are explainable.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.risk.liquidity import finite_float  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("audit_liquidity_panel")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"

AUDIT_FIELDS = [
    "ticker",
    "source_pipeline",
    "sector",
    "industry",
    "rating",
    "investable_eligible",
    "risk_eligible",
    "risk_status",
    "target_weight",
    "trade_notional",
    "median_half_spread_bps",
    "spread_vs_default_ratio",
    "latest_sample_date_et",
    "latest_sample_age_days",
    "valid_sample_count",
    "spread_source",
    "spread_status",
    "spread_reason",
    "liquidity_flag",
    "liquidity_reason",
    "commission_bps_of_position",
    "spread_cost_usd",
    "spread_cost_bps_of_aum",
]

SECTOR_FIELDS = [
    "source_pipeline",
    "sector",
    "n_tickers",
    "n_targets",
    "n_trades",
    "median_half_spread_bps",
    "p90_half_spread_bps",
    "max_half_spread_bps",
    "warn_spread_count",
    "fail_spread_count",
    "trade_spread_cost_usd",
]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Audit the IBKR liquidity spread snapshot.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _resolve_aum(config: dict[str, Any], cli_aum: float | None) -> float:
    aum = cli_aum if cli_aum is not None else cfg_get(config, "transaction_costs.aum_usd", None)
    if aum is None:
        raise ValueError("AUM is required: pass --aum or set transaction_costs.aum_usd in config")
    parsed = finite_float(aum, name="AUM")
    if parsed <= 0:
        raise ValueError(f"AUM must be positive, got {parsed}")
    return parsed


def _pct(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _map_by_ticker(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    for row in read_csv(path):
        ticker = str(row.get("ticker", "")).strip().upper()
        if ticker:
            out[ticker] = row
    return out


def _expected_liquidity_universe(run_dir: Path) -> set[str]:
    scores = {
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(run_dir / "stocks_scores.csv")
        if str(r.get("ticker", "")).strip() and str(r.get("investable_eligible", "")).strip() == "1"
    }
    meta_path = run_dir / "risk" / "spread_snapshot_meta.json"
    universe_source = ""
    if meta_path.exists():
        try:
            with meta_path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
            universe_source = str(meta.get("universe_source", "")).strip().lower()
        except (OSError, json.JSONDecodeError):
            universe_source = ""
    if "stocks_scores.csv:investable_eligible" in universe_source or "investable_scores" in universe_source:
        return scores

    coverage_path = run_dir / "risk" / "risk_coverage.csv"
    if not coverage_path.exists():
        return scores

    coverage = {
        str(r.get("ticker", "")).strip().upper()
        for r in read_csv(coverage_path)
        if (
            str(r.get("ticker", "")).strip()
            and str(r.get("role", "")).strip() == "scored"
            and str(r.get("score_eligible", "")).strip() == "1"
            and str(r.get("risk_eligible", "")).strip() == "1"
        )
    }
    return scores & coverage


def _read_spread_snapshot(path: Path) -> dict[str, dict[str, str]]:
    return _map_by_ticker(path)


def _checks(
    *,
    audit_rows: Sequence[dict[str, Any]],
    expected_universe: set[str],
    target_tickers: set[str],
    trade_tickers: set[str],
    snapshot_tickers: set[str],
    config: dict[str, Any],
    weighted_trade_spread_bps: float,
) -> list[dict[str, str]]:
    lp = cfg_get(config, "liquidity_panel", {}) or {}
    ac = lp.get("audit", {}) if isinstance(lp.get("audit"), dict) else {}
    warn_bps = finite_float(ac.get("half_spread_warn_bps", 50.0), name="liquidity_panel.audit.half_spread_warn_bps")
    fail_bps = finite_float(ac.get("half_spread_fail_bps", 1000.0), name="liquidity_panel.audit.half_spread_fail_bps")
    trade_warn = finite_float(
        ac.get("trade_weighted_half_spread_warn_bps", 20.0),
        name="liquidity_panel.audit.trade_weighted_half_spread_warn_bps",
    )
    trade_fail = finite_float(
        ac.get("trade_weighted_half_spread_fail_bps", 100.0),
        name="liquidity_panel.audit.trade_weighted_half_spread_fail_bps",
    )
    max_fallback = finite_float(cfg_get(config, "liquidity_panel.max_universe_fallback_fraction", 0.10),
                                name="liquidity_panel.max_universe_fallback_fraction")

    checks: list[dict[str, str]] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    missing_universe = sorted(expected_universe - snapshot_tickers)
    extra_snapshot = sorted(snapshot_tickers - expected_universe)
    rec(
        "liquidity_universe_coverage",
        "PASS" if not missing_universe else "FAIL",
        f"expected={len(expected_universe)} snapshot={len(snapshot_tickers)} extra={len(extra_snapshot)}"
        if not missing_universe else f"missing={missing_universe[:20]}",
    )

    missing_targets = sorted(target_tickers - snapshot_tickers)
    missing_trades = sorted(trade_tickers - snapshot_tickers)
    rec(
        "liquidity_optimizer_trade_coverage",
        "PASS" if not missing_targets and not missing_trades else "FAIL",
        f"targets={len(target_tickers)} trades={len(trade_tickers)} covered"
        if not missing_targets and not missing_trades else
        f"missing_targets={missing_targets[:10]} missing_trades={missing_trades[:10]}",
    )

    fallback = [r for r in audit_rows if str(r.get("spread_status")) == "fallback"]
    failed = [r for r in audit_rows if str(r.get("spread_status")) == "failed"]
    fallback_fraction = len(fallback) / len(audit_rows) if audit_rows else 0.0
    rec(
        "liquidity_fallback_fraction",
        "PASS" if fallback_fraction <= max_fallback + 1e-12 and not failed else "FAIL",
        f"fallback={len(fallback)}/{len(audit_rows)} ({fallback_fraction:.4f}), failed={len(failed)}, max={max_fallback}",
    )

    hard_extreme = [
        r for r in audit_rows
        if _safe_float(r.get("median_half_spread_bps")) >= fail_bps
    ]
    warn_extreme = [
        r for r in audit_rows
        if _safe_float(r.get("median_half_spread_bps")) >= warn_bps
    ]
    rec(
        "liquidity_extreme_spread_hard",
        "PASS" if not hard_extreme else "FAIL",
        f"no half-spreads >= {fail_bps} bps" if not hard_extreme else
        f"{len(hard_extreme)} tickers >= {fail_bps} bps: {[r['ticker'] for r in hard_extreme[:10]]}",
    )
    rec(
        "liquidity_extreme_spread_review",
        "PASS" if not warn_extreme else "WARN",
        f"no half-spreads >= {warn_bps} bps" if not warn_extreme else
        f"{len(warn_extreme)} tickers >= {warn_bps} bps: "
        f"{[(r['ticker'], r['median_half_spread_bps']) for r in warn_extreme[:10]]}",
    )

    trade_status = "PASS"
    if weighted_trade_spread_bps >= trade_fail:
        trade_status = "FAIL"
    elif weighted_trade_spread_bps >= trade_warn:
        trade_status = "WARN"
    rec(
        "liquidity_trade_weighted_spread",
        trade_status,
        f"weighted trade half-spread={weighted_trade_spread_bps:.4f} bps "
        f"(warn={trade_warn}, fail={trade_fail})",
    )

    return checks


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
    try:
        aum = _resolve_aum(config, args.aum)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    run_dir = runs_root / run_as_of
    risk_dir = run_dir / "risk"
    audit_path = risk_dir / "liquidity_audit.csv"
    sector_path = risk_dir / "liquidity_audit_by_sector.csv"
    summary_path = risk_dir / "liquidity_audit_summary.json"
    if args.force:
        for path in (
            audit_path,
            sector_path,
            summary_path,
            risk_dir / "risk_manifest.json",
            risk_dir / "validation" / "risk_panel_validation.csv",
        ):
            if path.exists() and path.is_file():
                path.unlink()
    try:
        fail_if_exists([audit_path, sector_path, summary_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    spread_path = risk_dir / "spread_snapshot.csv"
    if not spread_path.exists():
        LOGGER.error("Missing %s; run 05c_collect_ib_historical_spread_samples.py first", spread_path)
        return 1

    scores = _map_by_ticker(run_dir / "stocks_scores.csv")
    coverage = _map_by_ticker(risk_dir / "risk_coverage.csv")
    target_weights = _map_by_ticker(run_dir / "optimizer" / "target_weights.csv")
    trade_list = _map_by_ticker(run_dir / "costs" / "trade_list.csv")
    spread_rows = _read_spread_snapshot(spread_path)
    expected_universe = _expected_liquidity_universe(run_dir)
    default_spread = finite_float(cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
                                  name="transaction_costs.half_spread_bps_default")
    comm_base = finite_float(cfg_get(config, "transaction_costs.commission_per_order.base", 1.125),
                             name="transaction_costs.commission_per_order.base")

    audit_rows: list[dict[str, Any]] = []
    sector_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    total_trade_spread_cost = 0.0
    total_trade_notional = 0.0

    for ticker in sorted(spread_rows):
        spread = spread_rows[ticker]
        score = scores.get(ticker, {})
        cov = coverage.get(ticker, {})
        target = target_weights.get(ticker, {})
        trade = trade_list.get(ticker, {})
        spread_status = str(spread.get("spread_status", "")).strip().lower()
        spread_reason = str(spread.get("spread_reason", "")).strip()
        half_spread = _optional_float(spread.get("median_half_spread_bps"))
        missing_spread_reason = ""
        if half_spread is None:
            missing_spread_reason = (
                f"missing_or_invalid_median_half_spread_bps:"
                f"{spread.get('median_half_spread_bps')!r}"
            )
        half_spread_for_cost = half_spread if half_spread is not None else 0.0
        target_weight = _safe_float(target.get("weight"), 0.0)
        trade_notional = _safe_float(trade.get("trade_notional"), 0.0)
        spread_cost = half_spread_for_cost / 1e4 * trade_notional if trade_notional > 0 else 0.0
        total_trade_spread_cost += spread_cost
        total_trade_notional += trade_notional
        position_notional = target_weight * aum
        commission_bps = comm_base / position_notional * 1e4 if position_notional > 0 else 0.0

        flag = "ok"
        reason = ""
        warn_bps = _safe_float(cfg_get(config, "liquidity_panel.audit.half_spread_warn_bps", 50.0), 50.0)
        fail_bps = _safe_float(cfg_get(config, "liquidity_panel.audit.half_spread_fail_bps", 1000.0), 1000.0)
        if half_spread is None:
            flag = "fail_missing_spread"
            reason = spread_reason or missing_spread_reason
        elif half_spread >= fail_bps:
            flag = "fail_extreme_spread"
            reason = f"half_spread_bps>={fail_bps:g}"
        elif half_spread >= warn_bps:
            flag = "review_extreme_spread"
            reason = f"half_spread_bps>={warn_bps:g}"
        elif spread_status in {"fallback", "failed"}:
            flag = "review_fallback"
            reason = spread_reason

        row = {
            "ticker": ticker,
            "source_pipeline": score.get("source_pipeline", cov.get("source_pipeline", "")),
            "sector": score.get("sector", ""),
            "industry": score.get("industry", ""),
            "rating": score.get("rating", ""),
            "investable_eligible": score.get("investable_eligible", ""),
            "risk_eligible": cov.get("risk_eligible", ""),
            "risk_status": cov.get("risk_status", ""),
            "target_weight": round(target_weight, 10),
            "trade_notional": round(trade_notional, 4),
            "median_half_spread_bps": round(half_spread, 6) if half_spread is not None else "",
            "spread_vs_default_ratio": round(half_spread / default_spread, 6)
            if half_spread is not None and default_spread > 0 else "",
            "latest_sample_date_et": spread.get("latest_sample_date_et", ""),
            "latest_sample_age_days": spread.get("latest_sample_age_days", ""),
            "valid_sample_count": spread.get("valid_sample_count", ""),
            "spread_source": spread.get("spread_source", ""),
            "spread_status": spread.get("spread_status", ""),
            "spread_reason": spread.get("spread_reason", ""),
            "liquidity_flag": flag,
            "liquidity_reason": reason,
            "commission_bps_of_position": round(commission_bps, 6),
            "spread_cost_usd": round(spread_cost, 4),
            "spread_cost_bps_of_aum": round(spread_cost / aum * 1e4, 6) if aum > 0 else 0.0,
        }
        audit_rows.append(row)
        sector_groups[(str(row["source_pipeline"]), str(row["sector"]))].append(row)

    sector_rows = []
    for (source_pipeline, sector), rows in sorted(sector_groups.items()):
        values = [
            value for value in (_optional_float(r["median_half_spread_bps"]) for r in rows)
            if value is not None
        ]
        sector_rows.append({
            "source_pipeline": source_pipeline,
            "sector": sector,
            "n_tickers": len(rows),
            "n_targets": sum(1 for r in rows if _safe_float(r["target_weight"]) > 0),
            "n_trades": sum(1 for r in rows if _safe_float(r["trade_notional"]) > 0),
            "median_half_spread_bps": round(float(statistics.median(values)), 6) if values else 0.0,
            "p90_half_spread_bps": round(_pct(values, 0.90), 6),
            "max_half_spread_bps": round(max(values), 6) if values else 0.0,
            "warn_spread_count": sum(1 for r in rows if str(r["liquidity_flag"]) == "review_extreme_spread"),
            "fail_spread_count": sum(1 for r in rows if str(r["liquidity_flag"]) == "fail_extreme_spread"),
            "trade_spread_cost_usd": round(sum(_safe_float(r["spread_cost_usd"]) for r in rows), 4),
        })

    write_csv(audit_path, AUDIT_FIELDS, audit_rows)
    write_csv(sector_path, SECTOR_FIELDS, sector_rows)

    weighted_trade_spread_bps = total_trade_spread_cost / total_trade_notional * 1e4 if total_trade_notional > 0 else 0.0
    checks = _checks(
        audit_rows=audit_rows,
        expected_universe=expected_universe,
        target_tickers={t for t, r in target_weights.items() if _safe_float(r.get("weight")) > 0},
        trade_tickers={t for t, r in trade_list.items() if _safe_float(r.get("trade_notional")) > 0},
        snapshot_tickers=set(spread_rows),
        config=config,
        weighted_trade_spread_bps=weighted_trade_spread_bps,
    )
    hard_pass = all(c["status"] in {"PASS", "WARN"} for c in checks)
    top_spreads = sorted(audit_rows, key=lambda r: _safe_float(r["median_half_spread_bps"]), reverse=True)[:20]
    target_spreads = sorted(
        [r for r in audit_rows if _safe_float(r["target_weight"]) > 0],
        key=lambda r: _safe_float(r["median_half_spread_bps"]),
        reverse=True,
    )[:20]
    summary = {
        "run_as_of": run_as_of,
        "stage": "stage2_5_liquidity_audit",
        "generated_at": _timestamp(),
        "acceptance": "PASS" if hard_pass else "FAIL",
        "aum_usd": aum,
        "counts": {
            "snapshot_rows": len(spread_rows),
            "expected_universe": len(expected_universe),
            "target_positions": sum(1 for r in audit_rows if _safe_float(r["target_weight"]) > 0),
            "trade_rows": sum(1 for r in audit_rows if _safe_float(r["trade_notional"]) > 0),
            "review_extreme_spread": sum(1 for r in audit_rows if str(r["liquidity_flag"]) == "review_extreme_spread"),
            "fail_extreme_spread": sum(1 for r in audit_rows if str(r["liquidity_flag"]) == "fail_extreme_spread"),
        },
        "trade_cost": {
            "trade_notional": round(total_trade_notional, 4),
            "spread_cost_usd": round(total_trade_spread_cost, 4),
            "weighted_half_spread_bps": round(weighted_trade_spread_bps, 6),
        },
        "top_spreads": [
            {
                "ticker": r["ticker"],
                "source_pipeline": r["source_pipeline"],
                "sector": r["sector"],
                "median_half_spread_bps": r["median_half_spread_bps"],
                "target_weight": r["target_weight"],
                "trade_notional": r["trade_notional"],
                "liquidity_flag": r["liquidity_flag"],
            }
            for r in top_spreads
        ],
        "top_target_spreads": [
            {
                "ticker": r["ticker"],
                "source_pipeline": r["source_pipeline"],
                "sector": r["sector"],
                "median_half_spread_bps": r["median_half_spread_bps"],
                "target_weight": r["target_weight"],
                "trade_notional": r["trade_notional"],
                "spread_cost_usd": r["spread_cost_usd"],
            }
            for r in target_spreads
        ],
        "files": {
            "spread_snapshot.csv": {"sha256": sha256_file(spread_path), "rows": len(spread_rows)},
            "liquidity_audit.csv": {"sha256": sha256_file(audit_path), "rows": len(audit_rows)},
            "liquidity_audit_by_sector.csv": {"sha256": sha256_file(sector_path), "rows": len(sector_rows)},
        },
        "checks": checks,
    }
    write_manifest(summary_path, summary)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    LOGGER.info(
        "Liquidity audit: %d rows, %d review spreads, weighted trade half-spread %.2f bps -> %s",
        len(audit_rows), summary["counts"]["review_extreme_spread"], weighted_trade_spread_bps, audit_path,
    )
    return 0 if hard_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
