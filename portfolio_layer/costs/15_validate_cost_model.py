#!/usr/bin/env python3
"""Stage 4 - validate the cost overlay and seal a provenance-hashed cost_manifest.json."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.costs.cost_common import (  # noqa: E402
    commission, finite_float, invalidate_after_validation, require_same_aum, resolve_aum,
)
from portfolio_layer.risk.liquidity import effective_spread_uses_panel, load_spread_snapshot  # noqa: E402
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_cost_model")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Validate the Stage 4 cost overlay.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None)
    p.add_argument("--aum", type=float, default=None)
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def f(value: object, name: str) -> float:
    return finite_float(value, name=name)


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
        aum = resolve_aum(config, args.aum)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    run_dir = runs_root / run_as_of
    opt_dir = run_dir / "optimizer"
    risk_dir = run_dir / "risk"
    costs_dir = run_dir / "costs"
    adjusted_path = costs_dir / "cost_adjusted_target_weights.csv"
    decisions_path = costs_dir / "no_trade_decisions.csv"
    summary_path = costs_dir / "cost_summary.json"
    trade_path = costs_dir / "trade_list.csv"
    trade_meta_path = costs_dir / "trade_list_meta.json"
    report_path = costs_dir / "cost_report.csv"
    opt_manifest_path = opt_dir / "optimizer_manifest.json"
    target_path = opt_dir / "target_weights.csv"
    validation_path = costs_dir / "validation" / "cost_validation.csv"
    manifest_path = costs_dir / "cost_manifest.json"
    for required in (
        adjusted_path, decisions_path, summary_path, trade_path, trade_meta_path, report_path,
        opt_manifest_path, target_path,
    ):
        if not required.exists():
            LOGGER.error("Run 09/12/13/14 first; missing %s", required)
            return 1
    if args.force:
        invalidate_after_validation(costs_dir)
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    try:
        gross = f(cfg_get(config, "optimizer.gross_exposure", 1.0), "optimizer.gross_exposure")
        comm_base = commission(config, "base")
        comm_worst = commission(config, "worst_case")
        half_spread_bps = f(cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
                            "transaction_costs.half_spread_bps_default")
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    trade_meta = json.loads(trade_meta_path.read_text(encoding="utf-8"))
    opt_manifest = json.loads(opt_manifest_path.read_text(encoding="utf-8"))
    adjusted = read_csv(adjusted_path)
    trades = read_csv(trade_path)
    reports = read_csv(report_path)
    decisions = read_csv(decisions_path)

    checks: list[dict] = []

    def rec(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    # 1. AUM present, positive, and consistent across Stage 4 artifacts.
    aum_bad = []
    try:
        require_same_aum(aum, trade_meta.get("aum_usd"), source="trade_list_meta.json")
        require_same_aum(aum, summary.get("aum_usd"), source="cost_summary.json")
    except ValueError as exc:
        aum_bad.append(str(exc))
    rec("aum_required_and_consistent", "PASS" if not aum_bad else "FAIL",
        f"aum_usd={aum}" if not aum_bad else "; ".join(aum_bad))

    # 2. Adjusted book is finite, unique, nonnegative, has exactly one CASH line, and closes to gross.
    adjusted_bad = []
    tickers = [str(r.get("ticker", "")).strip() for r in adjusted]
    duplicates = sorted(t for t, count in Counter(tickers).items() if t and count > 1)
    if duplicates:
        adjusted_bad.append(f"duplicate_tickers={duplicates[:10]}")
    asset_sum = 0.0
    cash_values = []
    for r in adjusted:
        ticker = str(r.get("ticker", "")).strip()
        try:
            weight = f(r.get("weight"), f"adjusted:{ticker}.weight")
        except ValueError as exc:
            adjusted_bad.append(str(exc))
            continue
        if weight < -1e-10:
            adjusted_bad.append(f"{ticker}:negative_weight={weight}")
        if ticker.upper() == "CASH":
            cash_values.append(weight)
        else:
            asset_sum += weight
    cash = sum(cash_values)
    if len(cash_values) != 1:
        adjusted_bad.append(f"cash_lines={len(cash_values)}")
    if cash < -1e-10:
        adjusted_bad.append(f"negative_cash={cash}")
    if abs((asset_sum + cash) - gross) > 1e-6:
        adjusted_bad.append(f"sum={asset_sum + cash:.10f}!={gross}")
    rec("adjusted_book_valid", "PASS" if not adjusted_bad else "FAIL",
        f"sum(assets)={asset_sum:.8f} + cash={cash:.8f} == gross={gross}" if not adjusted_bad else "; ".join(adjusted_bad[:5]))

    # 3. Cost report rows exactly match trade rows and formulas.
    trade_by_ticker = {str(r["ticker"]).strip(): r for r in trades}
    report_by_ticker = {str(r["ticker"]).strip(): r for r in reports}
    cost_bad = []
    if set(trade_by_ticker) != set(report_by_ticker):
        cost_bad.append(f"ticker_mismatch trades_only={sorted(set(trade_by_ticker)-set(report_by_ticker))[:5]} "
                        f"reports_only={sorted(set(report_by_ticker)-set(trade_by_ticker))[:5]}")
    for ticker in sorted(set(trade_by_ticker) & set(report_by_ticker)):
        t = trade_by_ticker[ticker]
        r = report_by_ticker[ticker]
        notional = f(t["trade_notional"], f"trade:{ticker}.notional")
        n_orders = int(f(t["n_orders"], f"trade:{ticker}.n_orders"))
        row_spread_raw = r.get("half_spread_bps_used")
        row_half_spread = f(
            row_spread_raw if row_spread_raw not in (None, "") else half_spread_bps,
            f"report:{ticker}.half_spread_bps_used",
        )
        if row_half_spread < 0:
            cost_bad.append(f"{ticker}:negative_half_spread_bps={row_half_spread}")
            continue
        spread = round((row_half_spread / 1e4) * notional, 4)
        comm_b = round(comm_base * n_orders, 4)
        comm_w = round(comm_worst * n_orders, 4)
        total_b = round(comm_b + spread, 4)
        total_w = round(comm_w + spread, 4)
        checks_for_row = {
            "side": str(t["side"]) == str(r["side"]),
            "trade_notional": abs(f(r["trade_notional"], f"report:{ticker}.notional") - notional) <= 1e-4,
            "commission_base": abs(f(r["commission_base"], f"report:{ticker}.commission_base") - comm_b) <= 1e-4,
            "commission_worst": abs(f(r["commission_worst"], f"report:{ticker}.commission_worst") - comm_w) <= 1e-4,
            "half_spread_bps_used": row_spread_raw not in (None, ""),
            "spread_cost": abs(f(r["spread_cost"], f"report:{ticker}.spread") - spread) <= 1e-4,
            "total_cost_base": abs(f(r["total_cost_base"], f"report:{ticker}.total_base") - total_b) <= 1e-4,
            "total_cost_worst": abs(f(r["total_cost_worst"], f"report:{ticker}.total_worst") - total_w) <= 1e-4,
        }
        bad_fields = [name for name, ok in checks_for_row.items() if not ok]
        if bad_fields:
            cost_bad.append(f"{ticker}:{bad_fields}")
    rec("cost_report_matches_trade_list", "PASS" if not cost_bad else "FAIL",
        f"{len(reports)} cost rows match trade formulas" if not cost_bad else f"{cost_bad[:10]}")

    # 4. Summary totals agree with cost report and trade metadata.
    n_orders = sum(int(f(r["n_orders"], f"trade:{r['ticker']}.n_orders")) for r in trades)
    gross_traded_notional = round(sum(f(r["trade_notional"], f"trade:{r['ticker']}.trade_notional") for r in trades), 4)
    one_way_base = round(sum(f(r["total_cost_base"], f"report:{r['ticker']}.total_base") for r in reports), 4)
    one_way_worst = round(sum(f(r["total_cost_worst"], f"report:{r['ticker']}.total_worst") for r in reports), 4)
    expected_comm = round(comm_base * n_orders, 4)
    summary_bad = []
    if int(summary.get("n_orders", -1)) != n_orders:
        summary_bad.append(f"n_orders={summary.get('n_orders')}!={n_orders}")
    if int(trade_meta.get("n_orders", -1)) != n_orders:
        summary_bad.append(f"trade_meta_n_orders={trade_meta.get('n_orders')}!={n_orders}")
    if abs(f(trade_meta.get("gross_traded_notional"), "trade_meta.gross_traded_notional") - gross_traded_notional) > 0.01:
        summary_bad.append("trade_meta_gross_traded_notional_mismatch")
    if abs(f(summary.get("commission_total_base"), "summary.commission_total_base") - expected_comm) > 1e-4:
        summary_bad.append("commission_total_base_mismatch")
    if abs(f(summary.get("one_way_cost_base_usd"), "summary.one_way_cost_base_usd") - one_way_base) > 1e-4:
        summary_bad.append("one_way_cost_base_mismatch")
    if abs(f(summary.get("one_way_cost_worst_usd"), "summary.one_way_cost_worst_usd") - one_way_worst) > 1e-4:
        summary_bad.append("one_way_cost_worst_mismatch")
    rec("cost_summary_matches_report", "PASS" if not summary_bad else "FAIL",
        f"orders={n_orders}, one_way_base=${one_way_base}" if not summary_bad else f"{summary_bad}")

    # 5. Commission applied as a FLAT $/order (exact), not bps.
    flat_ok = abs(f(summary.get("commission_total_base"), "summary.commission_total_base") - expected_comm) <= 1e-6
    rec("commission_flat_exact", "PASS" if flat_ok else "FAIL",
        f"commission_total={summary.get('commission_total_base')} == {n_orders} orders x ${comm_base} = {expected_comm}")

    # 6. Cost decomposes into a FIXED (AUM-invariant) commission + row-level spread costs.
    #    The spread can be the config default or per-ticker liquidity snapshot, so validation must
    #    consume the spread actually recorded on each report row instead of assuming one global bps.
    comm_total = f(summary.get("commission_total_base"), "summary.commission_total_base")
    report_spread_total = round(sum(f(r["spread_cost"], f"report:{r['ticker']}.spread_cost") for r in reports), 4)
    summary_spread_total = f(summary.get("spread_cost_total", report_spread_total), "summary.spread_cost_total")
    spread_total = round(one_way_base - comm_total, 4)
    impact_none = str(summary.get("impact_model", "none")) == "none"
    decomp_tol = max(0.05, 1e-4 * len(reports))
    decomp_ok = (
        impact_none
        and abs(spread_total - report_spread_total) <= decomp_tol
        and abs(summary_spread_total - report_spread_total) <= decomp_tol
    )
    weighted_bps = f(summary.get("weighted_half_spread_bps", half_spread_bps), "summary.weighted_half_spread_bps")
    rec("commission_fixed_plus_row_spread", "PASS" if decomp_ok else "FAIL",
        f"commission=${comm_total:.2f} (AUM-invariant; bps@AUM={comm_total / aum * 1e4:.4f} scales 1/AUM) + "
        f"spread=${spread_total:.2f} == row_spread_sum=${report_spread_total:.2f}; "
        f"weighted_half_spread_bps={weighted_bps:.4f}; "
        f"impact_none={impact_none}")

    # 7. Spread source policy is enforced: disabled/default means every row uses the default; enabled means
    #    rows are backed by a sealed spread snapshot with bounded fallback.
    spread_policy = str(cfg_get(config, "transaction_costs.spread_source", "auto")).strip().lower()
    try:
        panel_expected = effective_spread_uses_panel(config, risk_dir.parent)
    except ValueError as exc:
        spread_bad = [str(exc)]
        panel_expected = False
    else:
        spread_bad = []
    if spread_policy not in {"auto", "config_default", "liquidity_panel"}:
        spread_bad.append(f"invalid_spread_source_policy={spread_policy}")
    if panel_expected:
        snapshot_path = risk_dir / "spread_snapshot.csv"
        if not snapshot_path.exists():
            spread_bad.append("missing_spread_snapshot.csv")
            snapshot_rows = {}
        else:
            snapshot_rows = load_spread_snapshot(snapshot_path)
            if summary.get("spread_snapshot_sha256") != sha256_file(snapshot_path):
                spread_bad.append("spread_snapshot_hash_mismatch")
        missing = sorted(set(report_by_ticker) - set(snapshot_rows))
        if missing:
            spread_bad.append(f"snapshot_missing_trade_tickers={missing[:10]}")
        fallback_fraction = f(summary.get("spread_fallback_fraction", 0.0), "summary.spread_fallback_fraction")
        max_fallback = f(cfg_get(config, "liquidity_panel.max_fallback_fraction", 0.10),
                         "liquidity_panel.max_fallback_fraction")
        if fallback_fraction > max_fallback + 1e-12:
            spread_bad.append(f"fallback_fraction={fallback_fraction}>{max_fallback}")
        if str(summary.get("spread_mode", "")) != "liquidity_panel":
            spread_bad.append(f"summary.spread_mode={summary.get('spread_mode')}")
    else:
        for r in reports:
            ticker = str(r.get("ticker", "")).strip()
            row_bps = f(r.get("half_spread_bps_used"), f"report:{ticker}.half_spread_bps_used")
            if abs(row_bps - half_spread_bps) > 1e-8:
                spread_bad.append(f"{ticker}:half_spread_bps_used={row_bps}!={half_spread_bps}")
            if str(r.get("spread_source", "")) != "config_default":
                spread_bad.append(f"{ticker}:spread_source={r.get('spread_source')}")
            if str(r.get("spread_status", "")) != "config_default":
                spread_bad.append(f"{ticker}:spread_status={r.get('spread_status')}")
        if str(summary.get("spread_mode", "config_default")) != "config_default":
            spread_bad.append(f"summary.spread_mode={summary.get('spread_mode')}")
    rec("spread_source_policy_enforced", "PASS" if not spread_bad else "FAIL",
        ("liquidity_panel" if panel_expected else "config_default") + " spread source policy satisfied"
        if not spread_bad else f"{spread_bad[:10]}")

    try:
        trade_warn_bps = f(
            cfg_get(config, "liquidity_panel.audit.trade_weighted_half_spread_warn_bps", 20.0),
            "liquidity_panel.audit.trade_weighted_half_spread_warn_bps",
        )
        trade_fail_bps = f(
            cfg_get(config, "liquidity_panel.audit.trade_weighted_half_spread_fail_bps", 100.0),
            "liquidity_panel.audit.trade_weighted_half_spread_fail_bps",
        )
        weighted_spread_bps = f(summary.get("weighted_half_spread_bps", 0.0), "summary.weighted_half_spread_bps")
        if weighted_spread_bps >= trade_fail_bps:
            trade_spread_status = "FAIL"
        elif weighted_spread_bps >= trade_warn_bps:
            trade_spread_status = "WARN"
        else:
            trade_spread_status = "PASS"
        rec(
            "trade_weighted_spread_within_policy",
            trade_spread_status,
            f"weighted_half_spread_bps={weighted_spread_bps:.4f} warn={trade_warn_bps} fail={trade_fail_bps}",
        )
    except ValueError as exc:
        rec("trade_weighted_spread_within_policy", "FAIL", str(exc))

    # 8. One-way cost is the default; round-trip present only as a labeled diagnostic.
    one_way = "one_way_cost_base_usd" in summary
    round_trip_diag = any("round_trip" in k and "DIAGNOSTIC" in k for k in summary)
    rec("one_way_default_roundtrip_diagnostic", "PASS" if one_way and round_trip_diag else "FAIL",
        f"one_way={one_way} round_trip_labeled_diagnostic={round_trip_diag}")

    # 9. No-trade decisions cover every trade ticker exactly once.
    decision_tickers = [str(r.get("ticker", "")).strip() for r in decisions]
    decision_counts = Counter(decision_tickers)
    decision_dupes = sorted(t for t, count in decision_counts.items() if t and count > 1)
    missing_decisions = sorted(set(trade_by_ticker) - set(decision_tickers))
    extra_decisions = sorted(set(decision_tickers) - set(trade_by_ticker))
    decision_ok = not (decision_dupes or missing_decisions or extra_decisions)
    rec("no_trade_decisions_cover_trades", "PASS" if decision_ok else "FAIL",
        f"decisions={len(decisions)} cover trades" if decision_ok else
        f"missing={missing_decisions[:10]} extra={extra_decisions[:10]} duplicates={decision_dupes[:10]}")

    # 10. Adjusted weights must agree with the no-trade decisions.
    adjusted_by_ticker = {
        str(r.get("ticker", "")).strip(): f(r.get("weight"), f"adjusted:{r.get('ticker')}.weight")
        for r in adjusted
        if str(r.get("ticker", "")).strip().upper() != "CASH"
    }
    target_weights = {
        str(r.get("ticker", "")).strip(): f(r.get("weight"), f"target:{r.get('ticker')}.weight")
        for r in read_csv(target_path)
        if f(r.get("weight"), f"target:{r.get('ticker')}.weight") > 0
    }
    decision_bad = []
    prior_kept = set()
    for d in decisions:
        ticker = str(d.get("ticker", "")).strip()
        decision = str(d.get("decision", "")).strip()
        prior_w = f(d.get("prior_weight") or 0.0, f"decision:{ticker}.prior_weight")
        target_w = f(d.get("target_weight") or 0.0, f"decision:{ticker}.target_weight")
        actual_w = adjusted_by_ticker.get(ticker, 0.0)
        raw_scale = d.get("budget_scale")
        budget_scale = f(1.0 if raw_scale in (None, "") else raw_scale, f"decision:{ticker}.budget_scale")
        if not 0.0 < budget_scale <= 1.0 + 1e-12:
            decision_bad.append(f"{ticker}:invalid_budget_scale={budget_scale}")
            continue
        if decision in ("open", "execute"):
            expected_pre_scale = target_w
        elif decision == "drop_to_cash":
            expected_pre_scale = 0.0
        elif decision == "suppress_keep_prior":
            expected_pre_scale = prior_w
            if prior_w > 0:
                prior_kept.add(ticker)
        else:
            decision_bad.append(f"{ticker}:unknown_decision={decision}")
            continue
        expected_w = expected_pre_scale * budget_scale
        raw_applied = d.get("applied_weight")
        if raw_applied in (None, ""):
            decision_bad.append(f"{ticker}:missing_applied_weight")
            continue
        recorded_applied = f(raw_applied, f"decision:{ticker}.applied_weight")
        if abs(recorded_applied - expected_w) > 1e-8:
            decision_bad.append(
                f"{ticker}:{decision} recorded={recorded_applied:.10f} expected={expected_w:.10f}"
            )
        if abs(actual_w - expected_w) > 1e-8:
            decision_bad.append(f"{ticker}:{decision} actual={actual_w:.10f} expected={expected_w:.10f}")
    allowed_adjusted = set(target_weights) | prior_kept
    extra_adjusted = sorted(set(adjusted_by_ticker) - allowed_adjusted)
    if extra_adjusted:
        decision_bad.append(f"extra_adjusted_tickers={extra_adjusted[:10]}")
    rec("adjusted_weights_match_decisions", "PASS" if not decision_bad else "FAIL",
        "adjusted weights agree with no-trade decisions" if not decision_bad else f"{decision_bad[:10]}")

    # 11. Built on a sealed, accepted Stage 3 book (acceptance PASS + target hash match).
    target_hash = sha256_file(target_path)
    manifest_target_hash = (opt_manifest.get("provenance_sha256") or {}).get("target_weights.csv")
    upstream_ok = opt_manifest.get("acceptance") == "PASS" and target_hash == manifest_target_hash
    rec("stage3_book_sealed", "PASS" if upstream_ok else "FAIL",
        f"optimizer_acceptance={opt_manifest.get('acceptance')} target_hash_match={target_hash == manifest_target_hash}")

    # 12. Trade list lineage is current: target/optimizer hashes, cost summary -> trade meta, and prior hash if used.
    lineage_bad = []
    opt_manifest_hash = sha256_file(opt_manifest_path)
    trade_meta_hash = sha256_file(trade_meta_path)
    if trade_meta.get("target_weights_sha256") != target_hash:
        lineage_bad.append("trade_list_target_hash_mismatch")
    if trade_meta.get("optimizer_manifest_sha256") != opt_manifest_hash:
        lineage_bad.append("trade_list_optimizer_manifest_hash_mismatch")
    if summary.get("trade_list_meta_sha256") != trade_meta_hash:
        lineage_bad.append("cost_summary_trade_meta_hash_mismatch")
    prior_source = str(trade_meta.get("prior_source", "cash"))
    prior_hash = trade_meta.get("prior_weights_sha256")
    if prior_source == "cash":
        if prior_hash not in (None, ""):
            lineage_bad.append("cash_prior_has_hash")
    else:
        prior_path = Path(prior_source)
        if not prior_path.exists():
            lineage_bad.append(f"prior_source_missing={prior_source}")
        elif sha256_file(prior_path) != prior_hash:
            lineage_bad.append("prior_weights_hash_mismatch")
    rec("stage4_lineage_current", "PASS" if not lineage_bad else "FAIL",
        "trade list/cost summary/prior lineage hashes match current artifacts" if not lineage_bad else f"{lineage_bad}")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, ["check", "status", "detail"], checks)
    hard = [c for c in checks if c["status"] != "WARN"]
    passed = all(c["status"] == "PASS" for c in hard)

    provenance_paths = {
        "target_weights.csv": target_path,
        "optimizer_manifest.json": opt_manifest_path,
        "covariance.csv": risk_dir / "covariance.csv",
        "stocks_scores.csv": run_dir / "stocks_scores.csv",
        "trade_list.csv": trade_path,
        "trade_list_meta.json": trade_meta_path,
        "cost_report.csv": report_path,
        "cost_summary.json": summary_path,
        "cost_adjusted_target_weights.csv": adjusted_path,
        "no_trade_decisions.csv": decisions_path,
        "spread_snapshot.csv": risk_dir / "spread_snapshot.csv",
        "spread_snapshot_meta.json": risk_dir / "spread_snapshot_meta.json",
        "validation/cost_validation.csv": validation_path,
        "config.yaml": config_path,
    }
    if not panel_expected:
        provenance_paths.pop("spread_snapshot.csv", None)
        provenance_paths.pop("spread_snapshot_meta.json", None)
    manifest = {
        "run_as_of": run_as_of,
        "stage": "stage4_cost_overlay",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "assumptions": {
            "aum_usd": aum,
            "commission_per_order": cfg_get(config, "transaction_costs.commission_per_order", {}),
            "decision_commission": cfg_get(config, "transaction_costs.decision_commission", "worst_case"),
            "rebalance_horizon_days": cfg_get(config, "transaction_costs.rebalance_horizon_days", 21),
            "enable_provisional_mu_no_trade": cfg_get(config, "transaction_costs.enable_provisional_mu_no_trade", False),
            "half_spread_bps_default": cfg_get(config, "transaction_costs.half_spread_bps_default", 5.0),
            "spread_source": cfg_get(config, "transaction_costs.spread_source", "auto"),
            "liquidity_panel": cfg_get(config, "liquidity_panel", {}),
            "impact_model": cfg_get(config, "transaction_costs.impact_model", "none"),
            "min_position_commission_fraction": cfg_get(config, "transaction_costs.min_position_commission_fraction", 0.005),
        },
        "cost_summary": summary,
        "cash_weight": round(cash, 10),
        "n_asset_positions": sum(1 for r in adjusted if str(r["ticker"]).upper() != "CASH"),
        "provenance_sha256": {n: sha256_file(p) for n, p in provenance_paths.items() if p.exists()},
        "checks": checks,
    }
    write_manifest(manifest_path, manifest)

    for c in checks:
        LOGGER.info("[%s] %s -- %s", c["status"], c["check"], c["detail"])
    if passed:
        LOGGER.info("STAGE 4 ACCEPTANCE: PASS (as_of=%s, cash=%.4f) -> %s", run_as_of, cash, manifest_path)
        return 0
    LOGGER.error("STAGE 4 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
