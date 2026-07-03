#!/usr/bin/env python3
"""Stage 9 - classify exit signals for ACTUAL ledger holdings (Phase 1 equities-only)."""
from __future__ import annotations

import argparse
import logging
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.exits.exit_common import (  # noqa: E402
    EXIT_SIGNAL_FIELDS,
    TARGET_GAP_FIELDS,
    UNSUPPORTED_FIELDS,
    bool_text,
    date_lag_days,
    f0,
    finite_float,
    i0,
    latest_run_on_or_before,
    load_json,
    manifest_hash_current,
    score_manifest_accepts,
    source_hashes,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("build_exit_signals")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["exit_common.py", "33_build_exit_signals.py"]


def finite_default(value: Any, default: float) -> float:
    parsed = finite_float(value)
    return default if parsed is None else parsed


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build Stage 9 exit signals over actual ledger holdings.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None, help="Ledger as-of date. Defaults to latest sealed ledger.")
    p.add_argument("--signal-as-of", type=iso_date_arg, default=None, help="Signal run date. Defaults to latest sealed scores <= ledger as-of.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _status_priority(action_hint: str, signal: str) -> int:
    if action_hint == "hard_exit":
        return 100
    if action_hint == "soft_exit":
        return 80
    if signal in {"large_loss_review", "large_position_review"}:
        return 60
    if action_hint == "review":
        return 50
    return 10


def _target_weights(signal_dir: Path) -> tuple[dict[str, float], str, Path | None]:
    sleeve_path = signal_dir / "sleeves" / "sleeve_adjusted_target_weights.csv"
    if sleeve_path.exists():
        rows = read_csv(sleeve_path)
        return {
            str(r.get("ticker", "")).strip().upper(): f0(r.get("weight"))
            for r in rows
            if str(r.get("ticker", "")).strip().upper() and str(r.get("ticker", "")).strip().upper() != "CASH"
        }, "stage8_sleeve_proposal", sleeve_path
    bl_path = signal_dir / "blacklitterman" / "costs" / "bl_cost_adjusted_target_weights.csv"
    if bl_path.exists():
        rows = read_csv(bl_path)
        return {
            str(r.get("ticker", "")).strip().upper(): f0(r.get("weight") or r.get("Weight"))
            for r in rows
            if str(r.get("ticker", "")).strip().upper() and str(r.get("ticker", "")).strip().upper() != "CASH"
        }, "stage7_bl_cost_adjusted", bl_path
    return {}, "none", None


def _lot_stats(lots: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for lot in lots:
        if lot.get("asset_category") != "Stocks":
            continue
        ticker = str(lot.get("symbol", "")).strip().upper()
        if not ticker:
            continue
        item = grouped.setdefault(ticker, {"lot_count": 0, "unknown": 0, "dates": []})
        item["lot_count"] += 1
        item["unknown"] += i0(lot.get("entry_date_unknown"))
        entry = str(lot.get("entry_date", "")).strip()
        if entry:
            item["dates"].append(entry)
    for item in grouped.values():
        dates = sorted(item["dates"])
        item["earliest_entry_date"] = dates[0] if dates else ""
    return grouped


def _classify(
    *,
    holding: dict[str, str],
    score: dict[str, str] | None,
    actual_weight: float,
    target_weight: float,
    lot_info: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, str]:
    ticker = str(holding.get("symbol", "")).strip().upper()
    qty = f0(holding.get("quantity"))
    market_value = f0(holding.get("market_value"))
    cost_basis = f0(holding.get("cost_basis"))
    unrealized_pl = f0(holding.get("unrealized_pl"))
    unrealized_return = unrealized_pl / cost_basis if abs(cost_basis) > 1e-12 else 0.0

    hard_ratings = {str(x).strip().lower() for x in (cfg_get(config, "exit_engine.score_decay.hard_exit_ratings", ["avoid"]) or [])}
    soft_ratings = {str(x).strip().lower() for x in (cfg_get(config, "exit_engine.score_decay.soft_exit_ratings", ["reduce"]) or [])}
    use_rating_for_exit = bool(cfg_get(config, "exit_engine.score_decay.use_rating_for_exit", False))
    hard_score = finite_float(cfg_get(config, "exit_engine.score_decay.hard_exit_final_score_below", -0.15))
    soft_score = finite_float(cfg_get(config, "exit_engine.score_decay.soft_exit_final_score_below", -0.10))
    review_score = finite_float(cfg_get(config, "exit_engine.score_decay.review_final_score_below", -0.05))
    min_conf = finite_float(cfg_get(config, "exit_engine.score_decay.min_score_confidence_review", 0.25))
    review_loss = finite_default(cfg_get(config, "exit_engine.position_risk.review_unrealized_loss_pct", -0.35), -0.35)
    review_weight = finite_default(cfg_get(config, "exit_engine.position_risk.review_actual_weight_pct", 0.15), 0.15)
    not_scored_action = str(cfg_get(config, "exit_engine.coverage_policy.not_scored_action", "review")).strip()
    not_investable_action = str(cfg_get(config, "exit_engine.coverage_policy.scored_not_investable_action", "soft_exit")).strip()

    source_pipeline = sector = rating = final_score_text = score_conf_text = investable_text = ""
    score_status = "held_not_scored"
    exit_signal = "not_scored_review"
    action_hint = not_scored_action if not_scored_action in {"keep", "review"} else "review"
    requires_review = True
    reason = "held ticker is absent from the latest sealed score contract; no score-decay exit possible"

    if score is not None:
        source_pipeline = str(score.get("source_pipeline", "")).strip()
        sector = str(score.get("sector", "")).strip()
        rating = str(score.get("rating", "")).strip().lower()
        final_score = finite_float(score.get("final_score"))
        score_conf = finite_float(score.get("score_confidence"))
        investable = i0(score.get("investable_eligible"))
        final_score_text = "" if final_score is None else f"{final_score:.12g}"
        score_conf_text = "" if score_conf is None else f"{score_conf:.12g}"
        investable_text = str(investable)

        if investable != 1:
            score_status = "scored_not_investable"
            exit_signal = "no_longer_investable"
            action_hint = not_investable_action if not_investable_action in {"soft_exit", "review"} else "soft_exit"
            requires_review = True
            reason = "held ticker is scored but no longer investable_eligible; candidate for soft exit"
        else:
            score_status = "scored_investable"
            exit_signal = "none"
            action_hint = "keep"
            requires_review = False
            reason = "held ticker remains scored and investable"
            if final_score is not None and hard_score is not None and final_score <= hard_score:
                exit_signal = "signal_decay_hard"
                action_hint = "hard_exit"
                requires_review = False
                reason = f"final_score={final_score:.4f} at/below hard-exit threshold {hard_score:.4f}"
            elif final_score is not None and soft_score is not None and final_score <= soft_score:
                exit_signal = "signal_decay_soft"
                action_hint = "soft_exit"
                requires_review = True
                reason = f"final_score={final_score:.4f} at/below soft-exit threshold {soft_score:.4f}"
            elif use_rating_for_exit and rating in hard_ratings:
                exit_signal = "signal_decay_hard"
                action_hint = "hard_exit"
                requires_review = False
                reason = f"rating={rating} is in hard-exit ratings"
            elif use_rating_for_exit and rating in soft_ratings:
                exit_signal = "signal_decay_soft"
                action_hint = "soft_exit"
                requires_review = True
                reason = f"rating={rating} is in soft-exit ratings"
            elif final_score is not None and review_score is not None and final_score < review_score:
                exit_signal = "weak_score_review"
                action_hint = "review"
                requires_review = True
                reason = f"final_score={final_score:.4f} below review threshold {review_score:.4f}"
            elif score_conf is not None and min_conf is not None and score_conf < min_conf:
                exit_signal = "low_confidence_review"
                action_hint = "review"
                requires_review = True
                reason = f"score_confidence={score_conf:.4f} below review threshold {min_conf:.4f}"

    if action_hint == "keep" and unrealized_return <= review_loss:
        exit_signal = "large_loss_review"
        action_hint = "review"
        requires_review = True
        reason = f"unrealized_return={unrealized_return:.2%} below review threshold {review_loss:.2%}"
    if action_hint == "keep" and actual_weight >= review_weight:
        exit_signal = "large_position_review"
        action_hint = "review"
        requires_review = True
        reason = f"actual_weight={actual_weight:.2%} above review threshold {review_weight:.2%}"

    return {
        "ledger_as_of": str(holding.get("run_as_of", "")),
        "signal_as_of": str(score.get("as_of_date", "") if score else ""),
        "ticker": ticker,
        "asset_category": "Stocks",
        "currency": str(holding.get("currency", "")),
        "quantity": f"{qty:.12g}",
        "market_value": f"{market_value:.12g}",
        "actual_weight": f"{actual_weight:.12g}",
        "target_weight": f"{target_weight:.12g}",
        "target_gap_weight": f"{target_weight - actual_weight:.12g}",
        "close_price": str(holding.get("close_price", "")),
        "cost_basis": f"{cost_basis:.12g}",
        "unrealized_pl": f"{unrealized_pl:.12g}",
        "unrealized_return": f"{unrealized_return:.12g}",
        "lot_count": str(int(lot_info.get("lot_count", 0))),
        "earliest_entry_date": str(lot_info.get("earliest_entry_date", "")),
        "entry_date_unknown_lots": str(int(lot_info.get("unknown", 0))),
        "source_pipeline": source_pipeline,
        "sector": sector,
        "rating": rating,
        "final_score": final_score_text,
        "score_confidence": score_conf_text,
        "investable_eligible": investable_text,
        "score_status": score_status,
        "holding_status": "actual_equity_holding",
        "exit_signal": exit_signal,
        "exit_priority": str(_status_priority(action_hint, exit_signal)),
        "action_hint": action_hint,
        "requires_review": bool_text(requires_review),
        "reason": reason,
    }


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"

    ledger_as_of = args.as_of or latest_run_with(runs_root, "ledger/ledger_manifest.json")
    if not ledger_as_of:
        LOGGER.error("No sealed holdings-ledger run found under %s", runs_root)
        return 1
    signal_as_of = args.signal_as_of or latest_run_on_or_before(runs_root, "manifest.json", ledger_as_of)
    if not signal_as_of:
        LOGGER.error("No sealed score run found on or before ledger_as_of=%s", ledger_as_of)
        return 1
    if date.fromisoformat(signal_as_of) > date.fromisoformat(ledger_as_of):
        LOGGER.error("signal_as_of=%s must be <= ledger_as_of=%s", signal_as_of, ledger_as_of)
        return 1

    ledger_dir = runs_root / ledger_as_of / "ledger"
    signal_dir = runs_root / signal_as_of
    exits_dir = runs_root / ledger_as_of / "exits"
    art = {
        "ledger_manifest.json": ledger_dir / "ledger_manifest.json",
        "holding_state.csv": ledger_dir / "holding_state.csv",
        "holding_lots.csv": ledger_dir / "holding_lots.csv",
        "broker_net_stock_positions.csv": ledger_dir / "broker_net_stock_positions.csv",
        "score_manifest.json": signal_dir / "manifest.json",
        "stocks_scores.csv": signal_dir / "stocks_scores.csv",
        "config.yaml": config_path,
    }
    missing = [name for name, path in art.items() if not path.exists()]
    if missing:
        LOGGER.error("Missing exit-signal inputs: %s", missing)
        return 1

    output_paths = {
        "exit_signals.csv": exits_dir / "exit_signals.csv",
        "target_gap_report.csv": exits_dir / "target_gap_report.csv",
        "unsupported_positions.csv": exits_dir / "unsupported_positions.csv",
        "exit_signals_meta.json": exits_dir / "exit_signals_meta.json",
    }
    if args.force:
        for path in output_paths.values():
            if path.exists():
                path.unlink()
    try:
        fail_if_exists(output_paths.values(), force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    ledger_manifest = load_json(art["ledger_manifest.json"])
    score_manifest = load_json(art["score_manifest.json"])
    lag_days = date_lag_days(signal_as_of, ledger_as_of)
    max_lag = int(finite_default(cfg_get(config, "exit_engine.max_signal_lag_days", 10), 10.0))
    checks: list[dict[str, str]] = []

    def rec(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    ledger_ok = ledger_manifest.get("acceptance") == "PASS"
    ledger_ok = ledger_ok and manifest_hash_current(ledger_manifest, rel_name="holding_state", path=art["holding_state.csv"])
    ledger_ok = ledger_ok and manifest_hash_current(ledger_manifest, rel_name="holding_lots", path=art["holding_lots.csv"])
    rec("ledger_sealed_current", "PASS" if ledger_ok else "FAIL", "Stage 8.5 ledger PASS and hashes current")

    score_ok = score_manifest_accepts(score_manifest)
    score_ok = score_ok and manifest_hash_current(score_manifest, rel_name="stocks_scores.csv", path=art["stocks_scores.csv"])
    rec("signal_sealed_current", "PASS" if score_ok else "FAIL", "Stage 1 score contract hard gates PASS and hash current")
    rec("signal_asof_lte_ledger_asof", "PASS" if lag_days >= 0 and lag_days <= max_lag else "FAIL",
        f"signal_as_of={signal_as_of}, ledger_as_of={ledger_as_of}, lag_days={lag_days}, max={max_lag}")

    if any(c["status"] == "FAIL" for c in checks):
        LOGGER.error("Upstream exit-signal gates failed: %s", checks)
        return 1

    holdings = read_csv(art["holding_state.csv"])
    lots = read_csv(art["holding_lots.csv"])
    scores = {str(r.get("ticker", "")).strip().upper(): r for r in read_csv(art["stocks_scores.csv"])}
    target_as_of = latest_run_on_or_before(runs_root, "sleeves/sleeve_manifest.json", ledger_as_of)
    target_dir = runs_root / target_as_of if target_as_of else signal_dir
    target_weights, target_source, target_path = _target_weights(target_dir)
    lot_info = _lot_stats(lots)
    stock_holdings = [r for r in holdings if r.get("asset_category") == "Stocks"]
    unsupported = [
        {
            "ledger_as_of": ledger_as_of,
            "signal_as_of": signal_as_of,
            "ticker": str(r.get("symbol", "")).strip().upper(),
            "asset_category": str(r.get("asset_category", "")),
            "quantity": str(r.get("quantity", "")),
            "market_value": str(r.get("market_value", "")),
            "unsupported_reason": str(cfg_get(config, "exit_engine.coverage_policy.option_action", "unsupported_phase1")),
        }
        for r in holdings
        if r.get("asset_category") != "Stocks"
    ]
    total_stock_mv = sum(max(0.0, f0(r.get("market_value"))) for r in stock_holdings)
    signal_rows: list[dict[str, str]] = []
    for holding in sorted(stock_holdings, key=lambda r: str(r.get("symbol", "")).strip().upper()):
        ticker = str(holding.get("symbol", "")).strip().upper()
        market_value = max(0.0, f0(holding.get("market_value")))
        actual_weight = market_value / total_stock_mv if total_stock_mv > 0 else 0.0
        target_weight = target_weights.get(ticker, 0.0)
        row = _classify(
            holding=holding,
            score=scores.get(ticker),
            actual_weight=actual_weight,
            target_weight=target_weight,
            lot_info=lot_info.get(ticker, {}),
            config=config,
        )
        row["ledger_as_of"] = ledger_as_of
        row["signal_as_of"] = signal_as_of
        signal_rows.append(row)

    by_ticker = {r["ticker"]: r for r in signal_rows}
    gap_rows: list[dict[str, str]] = []
    for ticker in sorted(set(by_ticker) | set(target_weights)):
        sig = by_ticker.get(ticker)
        actual_weight = f0(sig.get("actual_weight")) if sig else 0.0
        target_weight = target_weights.get(ticker, 0.0)
        gap_rows.append({
            "ledger_as_of": ledger_as_of,
            "signal_as_of": signal_as_of,
            "target_as_of": target_as_of or "",
            "ticker": ticker,
            "in_actual": bool_text(sig is not None),
            "in_target": bool_text(ticker in target_weights),
            "actual_weight": f"{actual_weight:.12g}",
            "target_weight": f"{target_weight:.12g}",
            "target_gap_weight": f"{target_weight - actual_weight:.12g}",
            "actual_quantity": sig.get("quantity", "0") if sig else "0",
            "market_value": sig.get("market_value", "0") if sig else "0",
            "score_status": sig.get("score_status", "target_only") if sig else "target_only",
            "action_hint": sig.get("action_hint", "none") if sig else "none",
            "target_source": target_source,
        })

    write_csv(output_paths["exit_signals.csv"], EXIT_SIGNAL_FIELDS, signal_rows)
    write_csv(output_paths["target_gap_report.csv"], TARGET_GAP_FIELDS, gap_rows)
    write_csv(output_paths["unsupported_positions.csv"], UNSUPPORTED_FIELDS, unsupported)

    counts = Counter(r["action_hint"] for r in signal_rows)
    score_status_counts = Counter(r["score_status"] for r in signal_rows)
    input_paths = {name: str(path) for name, path in art.items()}
    if target_path is not None:
        input_paths["target_gap_weights"] = str(target_path)
    meta = {
        "stage": "stage9_build_exit_signals",
        "phase": str(cfg_get(config, "exit_engine.phase", "phase1_actual_equity_holdings")),
        "ledger_as_of": ledger_as_of,
        "signal_as_of": signal_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "exit_engine.enabled_in_production", False)),
        "checks": checks,
        "row_counts": {
            "actual_stock_holdings": len(stock_holdings),
            "unsupported_positions": len(unsupported),
            "target_gap_rows": len(gap_rows),
        },
        "action_hint_counts": dict(sorted(counts.items())),
        "score_status_counts": dict(sorted(score_status_counts.items())),
        "target_gap_source": target_source,
        "target_as_of": target_as_of,
        "input_paths": input_paths,
        "inputs_sha256": {name: sha256_file(Path(path)) for name, path in input_paths.items() if Path(path).exists()},
        "outputs_sha256": {
            name: sha256_file(path)
            for name, path in output_paths.items()
            if name != "exit_signals_meta.json" and path.exists()
        },
        "source_sha256": source_hashes(PACKAGE_ROOT, "exits", SOURCE_FILES),
    }
    write_manifest(output_paths["exit_signals_meta.json"], meta)

    for check in checks:
        LOGGER.info("[%s] %s -- %s", check["status"], check["check"], check["detail"])
    LOGGER.info(
        "STAGE 9 EXIT SIGNALS: PASS (ledger=%s, signal=%s, stocks=%d, actions=%s, unsupported=%d)",
        ledger_as_of,
        signal_as_of,
        len(signal_rows),
        dict(sorted(counts.items())),
        len(unsupported),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
