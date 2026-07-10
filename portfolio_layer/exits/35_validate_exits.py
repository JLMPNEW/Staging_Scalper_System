#!/usr/bin/env python3
"""Stage 9 - validate and seal the actual-holdings exit proposal."""
from __future__ import annotations

import argparse
import importlib
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.artifacts import invalidate_dependents  # noqa: E402
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import fail_if_exists, read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.exits.exit_common import (  # noqa: E402
    EXIT_VALIDATION_FIELDS,
    date_lag_days,
    f0,
    finite_float,
    latest_run_on_or_before,
    load_json,
    manifest_hash_current,
    score_manifest_accepts,
    source_hashes,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("validate_exits")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["exit_common.py", "33_build_exit_signals.py", "34_apply_exits.py", "35_validate_exits.py"]


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
    p = argparse.ArgumentParser(description="Validate and seal Stage 9 exit proposal.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None, help="Ledger as-of date.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _verify_meta_hashes(meta: dict[str, Any], *, package_root: Path, output_root: Path) -> list[str]:
    bad: list[str] = []
    for name, expected in (meta.get("inputs_sha256") or {}).items():
        path_text = (meta.get("input_paths") or {}).get(name)
        if not path_text:
            bad.append(f"input_path_missing:{name}")
            continue
        path = Path(path_text)
        if not path.exists():
            bad.append(f"input_missing:{name}")
            continue
        if sha256_file(path) != expected:
            bad.append(f"input_hash:{name}")
    for name, expected in (meta.get("outputs_sha256") or {}).items():
        path = output_root / name
        if not path.exists():
            bad.append(f"output_missing:{name}")
            continue
        if sha256_file(path) != expected:
            bad.append(f"output_hash:{name}")
    for name, expected in (meta.get("source_sha256") or {}).items():
        path = package_root / "exits" / name
        if not path.exists():
            bad.append(f"source_missing:{name}")
            continue
        if sha256_file(path) != expected:
            bad.append(f"source_hash:{name}")
    return bad


def _by_ticker(rows: list[dict[str, str]], *, key: str = "ticker") -> dict[str, dict[str, str]]:
    return {str(row.get(key, "")).strip().upper(): row for row in rows if str(row.get(key, "")).strip()}


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    ledger_as_of = args.as_of or latest_run_with(runs_root, "exits/exit_actions_meta.json")
    if not ledger_as_of:
        LOGGER.error("No Stage 9 exit-action run found under %s", runs_root)
        return 1
    run_dir = runs_root / ledger_as_of
    exits_dir = run_dir / "exits"
    meta33_path = exits_dir / "exit_signals_meta.json"
    meta34_path = exits_dir / "exit_actions_meta.json"
    art = {
        "exit_signals": exits_dir / "exit_signals.csv",
        "exit_actions": exits_dir / "exit_actions.csv",
        "target_gap_report": exits_dir / "target_gap_report.csv",
        "unsupported_positions": exits_dir / "unsupported_positions.csv",
        "exit_summary": exits_dir / "exit_summary.json",
        "exit_signals_meta": meta33_path,
        "exit_actions_meta": meta34_path,
        "config": config_path,
    }
    missing = [name for name, path in art.items() if not path.exists()]
    if missing:
        LOGGER.error("Run 33 + 34 first; missing %s", missing)
        return 1

    validation_path = exits_dir / "validation" / "exit_validation.csv"
    manifest_path = exits_dir / "exit_manifest.json"
    if args.force:
        invalidate_dependents(run_dir, "exits")
        for path in (validation_path, manifest_path):
            if path.exists():
                path.unlink()
    try:
        fail_if_exists([validation_path, manifest_path], force=args.force)
    except FileExistsError as exc:
        LOGGER.error("%s", exc)
        return 1

    meta33 = load_json(meta33_path)
    meta34 = load_json(meta34_path)
    signal_as_of = str(meta33.get("signal_as_of") or latest_run_on_or_before(runs_root, "manifest.json", ledger_as_of) or "")
    signal_dir = runs_root / signal_as_of
    ledger_dir = runs_root / ledger_as_of / "ledger"
    upstream = {
        "ledger_manifest": ledger_dir / "ledger_manifest.json",
        "holding_state": ledger_dir / "holding_state.csv",
        "broker_net_stock_positions": ledger_dir / "broker_net_stock_positions.csv",
        "score_manifest": signal_dir / "manifest.json",
        "stocks_scores": signal_dir / "stocks_scores.csv",
    }
    missing_upstream = [name for name, path in upstream.items() if not path.exists()]
    if missing_upstream:
        LOGGER.error("Missing Stage 9 upstream inputs: %s", missing_upstream)
        return 1

    checks: list[dict[str, str]] = []

    def rec(check: str, status: str, detail: str) -> None:
        checks.append({"check": check, "status": status, "detail": detail})

    bad33 = []
    if meta33.get("acceptance") != "PASS":
        bad33.append(f"33_acceptance={meta33.get('acceptance')}")
    bad33.extend(_verify_meta_hashes(meta33, package_root=PACKAGE_ROOT, output_root=exits_dir))
    rec("stage33_exit_signals_current", "PASS" if not bad33 else "FAIL",
        "33 accepted and hashes/source current" if not bad33 else "; ".join(bad33[:8]))

    bad34 = []
    if meta34.get("acceptance") != "PASS":
        bad34.append(f"34_acceptance={meta34.get('acceptance')}")
    bad34.extend(_verify_meta_hashes(meta34, package_root=PACKAGE_ROOT, output_root=exits_dir))
    rec("stage34_exit_actions_current", "PASS" if not bad34 else "FAIL",
        "34 accepted and hashes/source current" if not bad34 else "; ".join(bad34[:8]))

    ledger_manifest = load_json(upstream["ledger_manifest"])
    ledger_ok = ledger_manifest.get("acceptance") == "PASS"
    ledger_ok = ledger_ok and manifest_hash_current(ledger_manifest, rel_name="holding_state", path=upstream["holding_state"])
    rec("ledger_current", "PASS" if ledger_ok else "FAIL",
        "Stage 8.5 ledger PASS and holding_state hash current" if ledger_ok else "ledger stale or failed")

    score_manifest = load_json(upstream["score_manifest"])
    score_ok = score_manifest_accepts(score_manifest)
    score_ok = score_ok and manifest_hash_current(score_manifest, rel_name="stocks_scores.csv", path=upstream["stocks_scores"])
    rec("signal_current", "PASS" if score_ok else "FAIL",
        "Stage 1 signal hard gates PASS and stocks_scores hash current" if score_ok else "score signal stale or failed")

    max_lag = int(round(finite_default(cfg_get(config, "exit_engine.max_signal_lag_days", 10), 10.0)))
    lag_days = date_lag_days(signal_as_of, ledger_as_of)
    rec("pit_signal_asof_lte_ledger_asof", "PASS" if lag_days >= 0 and lag_days <= max_lag else "FAIL",
        f"signal_as_of={signal_as_of}, ledger_as_of={ledger_as_of}, lag_days={lag_days}, max={max_lag}")

    holdings = read_csv(upstream["holding_state"])
    stock_holdings = [row for row in holdings if row.get("asset_category") == "Stocks"]
    option_holdings = [row for row in holdings if row.get("asset_category") != "Stocks"]
    signals = read_csv(art["exit_signals"])
    actions = read_csv(art["exit_actions"])
    unsupported = read_csv(art["unsupported_positions"])
    gaps = read_csv(art["target_gap_report"])
    summary = load_json(art["exit_summary"])

    stock_tickers = {str(row.get("symbol", "")).strip().upper() for row in stock_holdings}
    signal_tickers = [str(row.get("ticker", "")).strip().upper() for row in signals]
    action_tickers = [str(row.get("ticker", "")).strip().upper() for row in actions]
    signal_dupes = len(signal_tickers) - len(set(signal_tickers))
    action_dupes = len(action_tickers) - len(set(action_tickers))
    coverage_bad: list[str] = []
    if set(signal_tickers) != stock_tickers:
        coverage_bad.append(f"signals_missing_or_extra={sorted(stock_tickers ^ set(signal_tickers))[:8]}")
    if set(action_tickers) != stock_tickers:
        coverage_bad.append(f"actions_missing_or_extra={sorted(stock_tickers ^ set(action_tickers))[:8]}")
    if signal_dupes:
        coverage_bad.append(f"signal_dupes={signal_dupes}")
    if action_dupes:
        coverage_bad.append(f"action_dupes={action_dupes}")
    rec("every_actual_equity_has_one_action", "PASS" if not coverage_bad else "FAIL",
        f"stocks={len(stock_tickers)} signals={len(signals)} actions={len(actions)}" if not coverage_bad else "; ".join(coverage_bad[:8]))

    action_by_ticker = _by_ticker(actions)
    signal_by_ticker = _by_ticker(signals)
    not_scored_bad = []
    for ticker, signal in signal_by_ticker.items():
        if signal.get("score_status") == "held_not_scored":
            action = action_by_ticker.get(ticker, {}).get("action")
            if action not in {"keep", "review"}:
                not_scored_bad.append(f"{ticker}:{action}")
    rec("held_not_scored_not_force_sold", "PASS" if not not_scored_bad else "FAIL",
        "not-scored held names are keep/review only" if not not_scored_bad else "; ".join(not_scored_bad[:8]))

    unsupported_tickers = {str(row.get("ticker", "")).strip().upper() for row in unsupported}
    options_bad = []
    if len(unsupported) != len(option_holdings):
        options_bad.append(f"unsupported_rows={len(unsupported)} options={len(option_holdings)}")
    option_symbols = {str(row.get("symbol", "")).strip().upper() for row in option_holdings}
    if unsupported_tickers != option_symbols:
        options_bad.append(f"unsupported_symbol_mismatch={sorted(unsupported_tickers ^ option_symbols)[:8]}")
    if unsupported_tickers & set(action_tickers):
        options_bad.append(f"option_action_overlap={sorted(unsupported_tickers & set(action_tickers))[:8]}")
    rec("options_unsupported_phase1_not_traded", "PASS" if not options_bad else "FAIL",
        f"unsupported_options={len(unsupported)}" if not options_bad else "; ".join(options_bad[:8]))

    no_buy_bad = []
    for row in actions:
        action = str(row.get("action", ""))
        fraction = finite_float(row.get("proposed_exit_fraction"))
        qty = finite_float(row.get("proposed_exit_quantity"))
        notional = finite_float(row.get("notional_to_exit"))
        if action not in {"keep", "review", "soft_exit", "hard_exit"}:
            no_buy_bad.append(f"{row.get('ticker')}:action={action}")
        if fraction is None or fraction < -1e-12 or fraction > 1.0 + 1e-12:
            no_buy_bad.append(f"{row.get('ticker')}:fraction={row.get('proposed_exit_fraction')}")
        if qty is None or qty < -1e-9:
            no_buy_bad.append(f"{row.get('ticker')}:qty={row.get('proposed_exit_quantity')}")
        if notional is None or notional < -1e-6:
            no_buy_bad.append(f"{row.get('ticker')}:notional={row.get('notional_to_exit')}")
    rec("no_buys_generated", "PASS" if not no_buy_bad else "FAIL",
        "all actions are exits/reviews over actual holdings" if not no_buy_bad else "; ".join(no_buy_bad[:8]))

    target_only = [row for row in gaps if row.get("in_target") == "1" and row.get("in_actual") != "1"]
    target_only_tickers = {str(row.get("ticker", "")).strip() for row in target_only if str(row.get("ticker", "")).strip()}
    target_only_actions = sorted(target_only_tickers & set(action_tickers))
    rec("target_gap_diagnostic_only", "PASS" if not target_only_actions else "FAIL",
        f"target_only_rows={len(target_only)}; no target-only action rows" if not target_only_actions else f"target_only_actions={target_only_actions[:8]}")

    lending_bad: list[str] = []
    stock_state = {str(row.get("symbol", "")).strip().upper(): row for row in stock_holdings}
    for row in read_csv(upstream["broker_net_stock_positions"]):
        ticker = str(row.get("symbol", "")).strip().upper()
        lent = f0(row.get("shares_lent"))
        if abs(lent) <= 1e-9:
            continue
        shares_at_ib = f0(row.get("shares_at_ib"))
        state_qty = f0(stock_state.get(ticker, {}).get("quantity"))
        if abs(state_qty - shares_at_ib) > 1e-6:
            lending_bad.append(f"{ticker}:state={state_qty:.6g},shares_at_ib={shares_at_ib:.6g},lent={lent:.6g}")
    rec("securities_lending_exposure_uses_owned_shares", "PASS" if not lending_bad else "FAIL",
        "exit quantities use holding_state owned shares" if not lending_bad else "; ".join(lending_bad[:8]))

    price_probe_bad: list[str] = []
    try:
        classifier = getattr(importlib.import_module("portfolio_layer.exits.33_build_exit_signals"), "_classify")
        probe_holding = {
            "run_as_of": ledger_as_of,
            "symbol": "__PRICE_PROBE__",
            "currency": "USD",
            "quantity": "10",
            "market_value": "500",
            "close_price": "50",
            "cost_basis": "1000",
            "unrealized_pl": "-500",
        }
        probe_score = {
            "as_of_date": signal_as_of,
            "source_pipeline": "probe",
            "sector": "probe",
            "rating": "buy",
            "final_score": "0.05",
            "score_confidence": "1.0",
            "investable_eligible": "1",
        }
        probe = classifier(
            holding=probe_holding,
            score=probe_score,
            actual_weight=0.01,
            target_weight=0.01,
            lot_info={"lot_count": 1, "unknown": 0, "earliest_entry_date": "2026-01-01"},
            config=config,
        )
        if probe.get("exit_signal") != "large_loss_review":
            price_probe_bad.append(f"exit_signal={probe.get('exit_signal')}")
        if probe.get("action_hint") not in {"keep", "review"}:
            price_probe_bad.append(f"action_hint={probe.get('action_hint')}")
    except Exception as exc:  # noqa: BLE001 - validation should report probe failure, not crash mid-run.
        price_probe_bad.append(f"{type(exc).__name__}:{exc}")
    rec("price_only_move_does_not_force_exit", "PASS" if not price_probe_bad else "FAIL",
        "scored/investable synthetic large-loss position becomes review, not an exit"
        if not price_probe_bad else "; ".join(price_probe_bad[:8]))

    events_path = runs_root / ledger_as_of / str(cfg_get(config, "exit_engine.catalyst_events_csv", "events/catalyst_events.csv"))
    rec("catalyst_time_stops_phase1", "WARN" if not events_path.exists() else "PASS",
        "catalyst event contract absent; time-stops disabled in Phase 1" if not events_path.exists() else "catalyst event contract present")

    validation_path.parent.mkdir(parents=True, exist_ok=True)
    write_csv(validation_path, EXIT_VALIDATION_FIELDS, checks)
    passed = all(row["status"] in {"PASS", "WARN"} for row in checks)

    provenance = {
        "exit_signals.csv": art["exit_signals"],
        "exit_actions.csv": art["exit_actions"],
        "target_gap_report.csv": art["target_gap_report"],
        "unsupported_positions.csv": art["unsupported_positions"],
        "exit_summary.json": art["exit_summary"],
        "exit_signals_meta.json": art["exit_signals_meta"],
        "exit_actions_meta.json": art["exit_actions_meta"],
        "validation/exit_validation.csv": validation_path,
        "ledger/ledger_manifest.json": upstream["ledger_manifest"],
        "ledger/holding_state.csv": upstream["holding_state"],
        f"signals/{signal_as_of}/manifest.json": upstream["score_manifest"],
        f"signals/{signal_as_of}/stocks_scores.csv": upstream["stocks_scores"],
        "config.yaml": config_path,
    }
    manifest = {
        "stage": "stage9_exit_engine",
        "phase": str(cfg_get(config, "exit_engine.phase", "phase1_actual_equity_holdings")),
        "run_as_of": ledger_as_of,
        "ledger_as_of": ledger_as_of,
        "signal_as_of": signal_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "exit_engine.enabled_in_production", False)),
        "checks": checks,
        "row_counts": {
            "stock_holdings": len(stock_holdings),
            "option_holdings_unsupported": len(option_holdings),
            "exit_actions": len(actions),
            "target_gap_rows": len(gaps),
        },
        "summary": summary,
        "provenance_sha256": {name: sha256_file(path) for name, path in provenance.items() if path.exists()},
        "source_sha256": source_hashes(PACKAGE_ROOT, "exits", SOURCE_FILES),
    }
    write_manifest(manifest_path, manifest)

    for row in checks:
        LOGGER.info("[%s] %s -- %s", row["status"], row["check"], row["detail"])
    if passed:
        LOGGER.info(
            "STAGE 9 ACCEPTANCE: PASS (ledger=%s, signal=%s, actions=%s, proposed_exit=$%.2f)",
            ledger_as_of,
            signal_as_of,
            summary.get("action_counts"),
            f0(summary.get("proposed_exit_notional")),
        )
        return 0
    LOGGER.error("STAGE 9 ACCEPTANCE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
