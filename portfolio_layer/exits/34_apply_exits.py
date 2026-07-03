#!/usr/bin/env python3
"""Stage 9 - convert exit signals into a shadow exit-action proposal."""
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
    EXIT_ACTION_FIELDS,
    VALID_ACTIONS,
    f0,
    finite_float,
    latest_run_on_or_before,
    load_json,
    source_hashes,
)
from portfolio_layer.risk.readiness import latest_run_with  # noqa: E402


LOGGER = logging.getLogger("apply_exits")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
SOURCE_FILES = ["exit_common.py", "33_build_exit_signals.py", "34_apply_exits.py"]


def iso_date_arg(raw: str) -> str:
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {raw!r}") from exc
    return raw


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Apply Stage 9 exit signals into a shadow action proposal.")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--as-of", type=iso_date_arg, default=None, help="Ledger as-of date.")
    p.add_argument("--force", action="store_true")
    return p.parse_args()


def _action_fraction(action: str, config: dict[str, Any]) -> float:
    if action == "hard_exit":
        parsed = finite_float(cfg_get(config, "exit_engine.actions.hard_exit_fraction", 1.0))
        return 1.0 if parsed is None else parsed
    if action == "soft_exit":
        parsed = finite_float(cfg_get(config, "exit_engine.actions.soft_exit_fraction", 0.5))
        return 0.5 if parsed is None else parsed
    return 0.0


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    runs_root = paths.output_dir / "runs"
    ledger_as_of = args.as_of or latest_run_with(runs_root, "exits/exit_signals_meta.json")
    if not ledger_as_of:
        LOGGER.error("No Stage 9 exit-signal run found under %s", runs_root)
        return 1
    exits_dir = runs_root / ledger_as_of / "exits"
    signal_meta_path = exits_dir / "exit_signals_meta.json"
    signals_path = exits_dir / "exit_signals.csv"
    unsupported_path = exits_dir / "unsupported_positions.csv"
    if not signal_meta_path.exists() or not signals_path.exists():
        LOGGER.error("Run 33 first; missing exit signals under %s", exits_dir)
        return 1

    signal_meta = load_json(signal_meta_path)
    signal_as_of = str(signal_meta.get("signal_as_of") or latest_run_on_or_before(runs_root, "manifest.json", ledger_as_of) or "")
    output_paths = {
        "exit_actions.csv": exits_dir / "exit_actions.csv",
        "exit_summary.json": exits_dir / "exit_summary.json",
        "exit_actions_meta.json": exits_dir / "exit_actions_meta.json",
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

    bad_inputs: list[str] = []
    if signal_meta.get("acceptance") != "PASS":
        bad_inputs.append(f"33_acceptance={signal_meta.get('acceptance')}")
    recorded = (signal_meta.get("outputs_sha256") or {}).get("exit_signals.csv")
    if recorded != sha256_file(signals_path):
        bad_inputs.append("exit_signals_hash")
    if bad_inputs:
        LOGGER.error("Stage 33 is not current: %s", bad_inputs)
        return 1

    rows = read_csv(signals_path)
    actions: list[dict[str, str]] = []
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        action = str(row.get("action_hint", "review")).strip()
        if action not in VALID_ACTIONS:
            action = "review"
        qty = f0(row.get("quantity"))
        market_value = max(0.0, f0(row.get("market_value")))
        unrealized_pl = f0(row.get("unrealized_pl"))
        fraction = max(0.0, min(1.0, _action_fraction(action, config)))
        exit_qty = qty * fraction
        notional = market_value * fraction
        estimated_realized_pl = unrealized_pl * fraction
        review = str(row.get("requires_review", "0")).strip() == "1" or action in {"soft_exit", "review"}
        actions.append({
            "ledger_as_of": ledger_as_of,
            "signal_as_of": signal_as_of,
            "ticker": ticker,
            "action": action,
            "exit_signal": str(row.get("exit_signal", "")),
            "exit_priority": str(row.get("exit_priority", "")),
            "quantity": f"{qty:.12g}",
            "proposed_exit_fraction": f"{fraction:.12g}",
            "proposed_exit_quantity": f"{exit_qty:.12g}",
            "market_value": f"{market_value:.12g}",
            "notional_to_exit": f"{notional:.12g}",
            "estimated_realized_pl": f"{estimated_realized_pl:.12g}",
            "requires_review": "1" if review else "0",
            "reason": str(row.get("reason", "")),
        })

    write_csv(output_paths["exit_actions.csv"], EXIT_ACTION_FIELDS, actions)
    counts = Counter(row["action"] for row in actions)
    by_signal = Counter(row["exit_signal"] for row in actions)
    transform_errors: list[str] = []
    if len(actions) != len(rows):
        transform_errors.append(f"actions={len(actions)} signals={len(rows)}")
    if not rows:
        transform_errors.append("no_signal_rows")
    for row in actions:
        action = row["action"]
        fraction = f0(row["proposed_exit_fraction"])
        exit_qty = f0(row["proposed_exit_quantity"])
        notional = f0(row["notional_to_exit"])
        if action not in VALID_ACTIONS:
            transform_errors.append(f"{row['ticker']}:invalid_action={action}")
        if fraction < -1e-12 or fraction > 1.0 + 1e-12:
            transform_errors.append(f"{row['ticker']}:fraction={fraction:.6g}")
        if exit_qty < -1e-9:
            transform_errors.append(f"{row['ticker']}:exit_qty={exit_qty:.6g}")
        if notional < -1e-6:
            transform_errors.append(f"{row['ticker']}:notional={notional:.6g}")
        if action in {"keep", "review"} and abs(fraction) > 1e-12:
            transform_errors.append(f"{row['ticker']}:non_exit_action_fraction={fraction:.6g}")
        if action in {"soft_exit", "hard_exit"} and fraction <= 0.0:
            transform_errors.append(f"{row['ticker']}:exit_action_zero_fraction")
    passed = not transform_errors
    summary: dict[str, Any] = {
        "stage": "stage9_apply_exits_summary",
        "ledger_as_of": ledger_as_of,
        "signal_as_of": signal_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "exit_engine.enabled_in_production", False)),
        "stock_actions": len(actions),
        "action_counts": dict(sorted(counts.items())),
        "exit_signal_counts": dict(sorted(by_signal.items())),
        "proposed_exit_notional": round(sum(f0(r["notional_to_exit"]) for r in actions), 6),
        "proposed_estimated_realized_pl": round(sum(f0(r["estimated_realized_pl"]) for r in actions), 6),
        "unsupported_positions": len(read_csv(unsupported_path)) if unsupported_path.exists() else 0,
        "transform_checks": [
            {
                "check": "exit_action_transform_valid",
                "status": "PASS" if passed else "FAIL",
                "detail": "all signal rows mapped to valid non-buy exit/review actions"
                if passed else "; ".join(transform_errors[:8]),
            }
        ],
        "WARNING": "shadow-only exit proposal; no broker orders are placed by Stage 9",
    }
    write_manifest(output_paths["exit_summary.json"], summary)

    input_paths = {
        "exit_signals_meta.json": str(signal_meta_path),
        "exit_signals.csv": str(signals_path),
        "unsupported_positions.csv": str(unsupported_path),
        "config.yaml": str(config_path),
    }
    meta = {
        "stage": "stage9_apply_exits",
        "phase": str(cfg_get(config, "exit_engine.phase", "phase1_actual_equity_holdings")),
        "ledger_as_of": ledger_as_of,
        "signal_as_of": signal_as_of,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "acceptance": "PASS" if passed else "FAIL",
        "shadow_only": True,
        "enabled_in_production": bool(cfg_get(config, "exit_engine.enabled_in_production", False)),
        "input_paths": input_paths,
        "inputs_sha256": {name: sha256_file(Path(path)) for name, path in input_paths.items() if Path(path).exists()},
        "outputs_sha256": {
            name: sha256_file(path)
            for name, path in output_paths.items()
            if name != "exit_actions_meta.json" and path.exists()
        },
        "summary": summary,
        "source_sha256": source_hashes(PACKAGE_ROOT, "exits", SOURCE_FILES),
    }
    write_manifest(output_paths["exit_actions_meta.json"], meta)
    LOGGER.info(
        "STAGE 9 EXIT ACTIONS: %s (ledger=%s, signal=%s, actions=%s, proposed_exit=$%.2f)",
        "PASS" if passed else "FAIL",
        ledger_as_of,
        signal_as_of,
        dict(sorted(counts.items())),
        summary["proposed_exit_notional"],
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
