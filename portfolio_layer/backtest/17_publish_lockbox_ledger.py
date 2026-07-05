#!/usr/bin/env python3
"""Stage 11 - publish the append-only lockbox OOS ledger at the Open Event (runs ONCE).

Replays the registered arms over the SEALED-WINDOW snapshots (>= sealed_start) with the identical
walk-forward engine and real state providers used by backtest/16 on the dev window, and seals the
result as the one-time out-of-sample evidence record.

FAIL-CLOSED GATES (all must hold, in order):
  1. config `stage11_lockbox.lockbox_opened: true`
  2. docs/LOCKBOX_PROTOCOL.md Amendment Log contains a dated "Open Event" entry
  3. one-open policy: the ledger directory must not already exist — there is NO --force; a second
     invocation refuses permanently (re-running the analysis after inspection would be tuning on
     the sealed window, which is exactly what the protocol forbids)

Output: output/lockbox_ledger/
  oos_arm_comparison.csv     sealed-window per-arm net-of-cost evidence
  oos_daily_curves.csv       sealed-window net daily curves per arm
  dev_arm_comparison.csv     copy of the latest dev-window 16 output for side-by-side reading
  lockbox_ledger_manifest.json  protocol sha, open-event text, input shas, content chain hash

`--dry-run` verifies the gates and prints the plan without writing anything (it does NOT run the
replay — even computing sealed results without sealing them would leak them for tuning).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402

from portfolio_layer.backtest.walkforward_common import (  # noqa: E402
    ARM_FIELDS, ARMS, build_real_providers, run_walkforward, summarize_arms,
)
from portfolio_layer.core.config import cfg_get, load_yaml  # noqa: E402
from portfolio_layer.core.contracts import read_csv, sha256_file, write_csv, write_manifest  # noqa: E402
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import open_macro_serving_db  # noqa: E402
from portfolio_layer.macro.taxonomy import sleeve_taxonomy  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox  # noqa: E402


LOGGER = logging.getLogger("publish_lockbox_ledger")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
OPEN_EVENT_RE = re.compile(r"^\s*-\s*\*\*(\d{4}-\d{2}-\d{2})[^*]*Open Event", re.MULTILINE | re.IGNORECASE)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish the one-time lockbox OOS ledger (Open Event only).")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    p.add_argument("--dry-run", action="store_true",
                   help="Verify the gates and print the plan; computes and writes NOTHING.")
    return p.parse_args()


def main() -> int:  # noqa: C901
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    try:
        lockbox = load_lockbox(config, config_path)
    except ValueError as exc:
        LOGGER.error("%s", exc)
        return 1

    # gate 1: config flag
    if not lockbox["lockbox_opened"]:
        LOGGER.error("stage11_lockbox.lockbox_opened is false. Opening requires a dated Open Event "
                     "entry in the protocol Amendment Log FIRST, then flipping the config mirror.")
        return 1
    # gate 2: dated Open Event entry in the protocol document
    protocol_text = Path(lockbox["protocol_path"]).read_text(encoding="utf-8")
    open_match = OPEN_EVENT_RE.search(protocol_text)
    if not open_match:
        LOGGER.error("No dated 'Open Event' entry found in the protocol Amendment Log; refusing "
                     "(config lockbox_opened=true without a protocol entry is a protocol violation).")
        return 1
    open_event_date = open_match.group(1)
    # gate 3: one-open policy
    ledger_dir = paths.output_dir / "lockbox_ledger"
    if ledger_dir.exists():
        LOGGER.error("Lockbox ledger already exists at %s. The ledger is published ONCE; there is "
                     "no --force by design.", ledger_dir)
        return 1

    store_dir = paths.output_dir / str(cfg_get(config, "snapshot_store.dir", "snapshot_store"))
    sealed = {}
    if store_dir.exists():
        for snap in sorted(store_dir.iterdir()):
            if (snap.is_dir() and (snap / "stocks_scores.csv").exists()
                    and lockbox["sealed_start"] <= snap.name <= open_event_date):
                sealed[snap.name] = snap
    if not sealed:
        LOGGER.error("No sealed-window snapshots in [%s..%s]; nothing to publish",
                     lockbox["sealed_start"], open_event_date)
        return 1
    panel_root = paths.output_dir / str(cfg_get(config, "survivorship_panel.dir", "survivorship_panel"))
    builds = sorted(p for p in panel_root.iterdir()
                    if p.is_dir() and (p / "survivorship_manifest.json").exists()) if panel_root.exists() else []
    if not builds:
        LOGGER.error("No survivorship panel build; run backtest/15b first")
        return 1
    panel_dir = builds[-1]

    LOGGER.info("OPEN EVENT %s: sealed snapshots=%d [%s..%s], panel=%s",
                open_event_date, len(sealed), min(sealed), max(sealed), panel_dir.name)
    if args.dry_run:
        LOGGER.info("Dry run: gates PASS; ledger would be published to %s (nothing computed)", ledger_dir)
        return 0

    panel_manifest = json.loads((panel_dir / "survivorship_manifest.json").read_text(encoding="utf-8"))
    if panel_manifest.get("acceptance") != "PASS":
        LOGGER.error("Survivorship panel %s acceptance=%s; refusing", panel_dir.name,
                     panel_manifest.get("acceptance"))
        return 1
    prices = pd.read_csv(panel_dir / "prices_adjclose.csv", index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]

    wf = cfg_get(config, "walkforward", {}) or {}
    params = dict(
        rebalance_every_n_snapshots=int(wf.get("rebalance_every_n_snapshots", 5)),
        one_way_cost_bps=float(wf.get("one_way_cost_bps", 5.0)),
        cov_lookback_trading_days=int(wf.get("cov_lookback_trading_days", 252)),
        cov_min_obs=int(wf.get("cov_min_obs", 60)),
        shrinkage_intensity=float(cfg_get(config, "risk_panel.shrinkage_intensity", 0.2)),
        max_universe=int(wf.get("max_universe", 150)),
        min_universe=int(wf.get("min_universe", 20)),
        use_confidence=bool(cfg_get(config, "optimizer.use_score_confidence", True)),
        risk_aversion=float(cfg_get(config, "optimizer.risk_aversion", 5.0)),
        max_weight=float(cfg_get(config, "optimizer.max_weight_per_name", 0.05)),
        min_weight=float(cfg_get(config, "optimizer.min_weight_to_hold", 0.002)),
        gross=float(cfg_get(config, "optimizer.gross_exposure", 1.0)),
        solver=str(cfg_get(config, "optimizer.solver", "ECOS")),
        macro_shift_scale=float(cfg_get(config, "black_litterman_fusion.macro_sector_shift_scale", 0.5)),
        macro_max_shift=float(cfg_get(config, "black_litterman_fusion.macro_sector_max_shift", 0.15)),
        rc_cap=float(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08)),
    )
    arms: list[str] = list(ARMS)
    snapshots = {d: read_csv(p / "stocks_scores.csv") for d, p in sealed.items()}
    taxonomy = sleeve_taxonomy(config)
    pipelines = [str(s.get("model_family")) for s in cfg_get(config, "score_contract.sectors", []) or []
                 if bool(s.get("enabled", True))]
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        regime_p, fits_p, rotation_p = build_real_providers(
            config, conn=conn, prices=prices, pipelines=pipelines, taxonomy=taxonomy)
        result = run_walkforward(snapshots=snapshots, prices=prices, arms=arms, params=params,
                                 regime_provider=regime_p, sector_fit_provider=fits_p,
                                 rotation_provider=rotation_p)
    finally:
        conn.close()
    arm_rows = summarize_arms(result, arms, verdict_cfg=wf)

    ledger_dir.mkdir(parents=True, exist_ok=False)
    arm_path = ledger_dir / "oos_arm_comparison.csv"
    curves_path = ledger_dir / "oos_daily_curves.csv"
    write_csv(arm_path, ARM_FIELDS, arm_rows)
    curve_rows = []
    for j, d in enumerate(result["day_index"]):
        row = {"date": d}
        for arm in arms:
            row[f"net_{arm}"] = round(result["net"][arm][j], 8)
        curve_rows.append(row)
    write_csv(curves_path, ["date"] + [f"net_{a}" for a in arms], curve_rows)

    dev_copy = ""
    dev_root = paths.output_dir / str(wf.get("dir", "walkforward"))
    dev_builds = sorted(p for p in dev_root.iterdir()
                        if p.is_dir() and (p / "arm_comparison.csv").exists()) if dev_root.exists() else []
    if dev_builds:
        shutil.copyfile(dev_builds[-1] / "arm_comparison.csv", ledger_dir / "dev_arm_comparison.csv")
        dev_copy = dev_builds[-1].name

    content_chain = hashlib.sha256()
    for p in (arm_path, curves_path):
        content_chain.update(sha256_file(p).encode())
    manifest = {
        "stage": "stage11_lockbox_ledger",
        "generated_at": utc_now(),
        "append_only": True,
        "open_event_date": open_event_date,
        "protocol_sha256": lockbox["protocol_sha256"],
        "sealed_window": [min(sealed), max(sealed)],
        "sealed_snapshots": {d: sha256_file(p / "stocks_scores.csv") for d, p in sealed.items()},
        "panel_build": panel_dir.name,
        "panel_manifest_sha256": sha256_file(panel_dir / "survivorship_manifest.json"),
        "dev_comparison_build": dev_copy,
        "params": params,
        "arms": arms,
        "content_chain_sha256": content_chain.hexdigest(),
        "files": {
            "oos_arm_comparison.csv": {"sha256": sha256_file(arm_path), "rows": len(arm_rows)},
            "oos_daily_curves.csv": {"sha256": sha256_file(curves_path), "rows": len(curve_rows)},
        },
    }
    write_manifest(ledger_dir / "lockbox_ledger_manifest.json", manifest)
    for r in arm_rows:
        LOGGER.info("OOS ARM %-10s net_sharpe=%s net_ir=%s active_t=%s promotable=%s",
                    r["arm"], r["net_sharpe"], r["net_ir_vs_baseline"], r["active_t"], r["promotable"])
    LOGGER.info("LOCKBOX LEDGER PUBLISHED (once): %s", ledger_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
