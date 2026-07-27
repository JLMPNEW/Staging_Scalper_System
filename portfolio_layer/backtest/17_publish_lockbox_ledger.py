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
import logging
import os
import re
import shutil
import sys
import tempfile
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
from portfolio_layer.core.contracts import (  # noqa: E402
    manifest_accepts,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.db import utc_now  # noqa: E402
from portfolio_layer.core.logging_utils import configure_utc_logging  # noqa: E402
from portfolio_layer.core.paths import resolve_runtime_paths  # noqa: E402
from portfolio_layer.macro.contract import open_macro_serving_db  # noqa: E402
from portfolio_layer.macro.taxonomy import sleeve_taxonomy  # noqa: E402
from portfolio_layer.research.stage11_common import load_lockbox, manifest_file_errors  # noqa: E402


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
    sealed: dict[str, Path] = {}
    snapshot_errors: list[str] = []
    if store_dir.exists():
        for snap in sorted(store_dir.iterdir()):
            if (snap.is_dir() and (snap / "stocks_scores.csv").exists()
                    and lockbox["sealed_start"] <= snap.name <= open_event_date):
                meta_path = snap / "snapshot_meta.json"
                source_manifest_path = snap / "manifest.json"
                try:
                    meta = read_manifest(meta_path)
                except ValueError as exc:
                    snapshot_errors.append(f"{snap.name}:{exc}")
                    continue
                if not manifest_accepts(meta):
                    snapshot_errors.append(f"{snap.name}:acceptance={meta.get('acceptance')}")
                if str(meta.get("as_of_date", "")) != snap.name:
                    snapshot_errors.append(f"{snap.name}:meta_as_of={meta.get('as_of_date')}")
                if str(meta.get("stocks_scores_sha256", "")) != sha256_file(snap / "stocks_scores.csv"):
                    snapshot_errors.append(f"{snap.name}:stocks_scores_hash_mismatch")
                if not source_manifest_path.exists() or str(meta.get("manifest_sha256", "")) != sha256_file(
                    source_manifest_path
                ):
                    snapshot_errors.append(f"{snap.name}:source_manifest_hash_mismatch")
                if len(str(meta.get("protocol_sha256", ""))) != 64:
                    snapshot_errors.append(f"{snap.name}:protocol_hash_missing")
                sealed[snap.name] = snap
    if snapshot_errors:
        LOGGER.error("Sealed snapshot-store integrity failures: %s", snapshot_errors[:12])
        return 1
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
    panel_manifest_path = panel_dir / "survivorship_manifest.json"
    panel_manifest = read_manifest(panel_manifest_path)
    if not manifest_accepts(panel_manifest, allow_deferred=False):
        LOGGER.error("Survivorship panel %s acceptance=%s; refusing", panel_dir.name,
                     panel_manifest.get("acceptance"))
        return 1
    prices_path = panel_dir / "prices_adjclose.csv"
    panel_errors = manifest_file_errors(panel_manifest, {"prices_adjclose.csv": prices_path})
    if panel_errors:
        LOGGER.error("Survivorship panel %s is stale/unsealed: %s", panel_dir.name, panel_errors)
        return 1
    prices = pd.read_csv(prices_path, index_col=0)
    prices.columns = [str(c).strip().upper() for c in prices.columns]
    prices.index = [str(idx)[:10] for idx in prices.index]
    prices = prices.loc[[idx <= open_event_date for idx in prices.index]]
    if prices.empty or str(prices.index[-1]) > open_event_date:
        LOGGER.error("Lockbox price panel could not be truncated at Open Event %s", open_event_date)
        return 1
    if args.dry_run:
        LOGGER.info("Dry run: integrity gates PASS; ledger would be published to %s (nothing computed)", ledger_dir)
        return 0

    wf = cfg_get(config, "walkforward", {}) or {}
    supportive_raw = wf.get("regime_gate_supportive_regimes")
    if supportive_raw is None:
        supportive_raw = ["HEATING_UP"]
    params = dict(
        rebalance_every_n_snapshots=int(wf.get("rebalance_every_n_snapshots", 5)),
        one_way_cost_bps=float(wf.get("one_way_cost_bps", 5.0)),
        cov_lookback_trading_days=int(wf.get("cov_lookback_trading_days", 252)),
        cov_min_obs=int(wf.get("cov_min_obs", 60)),
        shrinkage_intensity=float(cfg_get(config, "risk_panel.shrinkage_intensity", 0.2)),
        max_universe=int(wf.get("max_universe", 150)),
        min_universe=int(wf.get("min_universe", 20)),
        use_confidence=bool(cfg_get(config, "optimizer.use_confidence_adjusted_mu", True)),
        risk_aversion=float(cfg_get(config, "optimizer.risk_aversion", 5.0)),
        max_weight=float(cfg_get(config, "optimizer.max_weight_per_name", 0.05)),
        min_weight=float(cfg_get(config, "optimizer.min_weight_to_hold", 0.002)),
        gross=float(cfg_get(config, "optimizer.gross_exposure", 1.0)),
        solver=str(cfg_get(config, "optimizer.solver", "ECOS")),
        macro_shift_scale=float(cfg_get(config, "black_litterman_fusion.macro_sector_shift_scale", 0.5)),
        macro_max_shift=float(cfg_get(config, "black_litterman_fusion.macro_sector_max_shift", 0.15)),
        rc_cap=float(cfg_get(config, "sleeves.per_name_risk_contribution_cap", 0.08)),
        regime_gate_supportive_regimes=[str(s) for s in supportive_raw],
        regime_lever_mu_multiplier=float(wf.get("regime_lever_mu_multiplier", 1.5)),
        regime_lever_unsupported_mode=str(wf.get("regime_lever_unsupported_mode", "min_var")),
    )
    arms = [a for a in (wf.get("arms") or list(ARMS)) if a in ARMS]
    if "aqr_only" not in arms:
        arms = ["aqr_only", *arms]
    snapshots = {d: read_csv(p / "stocks_scores.csv") for d, p in sealed.items()}
    taxonomy = sleeve_taxonomy(config)
    pipelines = [str(s.get("model_family")) for s in cfg_get(config, "score_contract.sectors", []) or []
                 if bool(s.get("enabled", True))]
    macro_db_hash_before = sha256_file(paths.macro_serving_db_path)
    conn = open_macro_serving_db(paths.macro_serving_db_path)
    try:
        regime_p, fits_p, rotation_p = build_real_providers(
            config, conn=conn, prices=prices, pipelines=pipelines, taxonomy=taxonomy)
        result = run_walkforward(snapshots=snapshots, prices=prices, arms=arms, params=params,
                                 regime_provider=regime_p, sector_fit_provider=fits_p,
                                 rotation_provider=rotation_p)
    finally:
        conn.close()
    macro_db_hash_after = sha256_file(paths.macro_serving_db_path)
    if macro_db_hash_before != macro_db_hash_after:
        LOGGER.error("Macro serving DB changed during the one-time lockbox replay; refusing publication")
        return 1
    replay_errors: list[str] = []
    if result["pit_violations"]:
        replay_errors.append(f"pit_violations={result['pit_violations'][:8]}")
    pit_checked = int(result.get("pit_boundaries_checked", 0))
    if pit_checked < int(result["n_rebalances"]) or pit_checked == 0:
        replay_errors.append(
            f"pit_boundaries_checked={pit_checked}<executed_rebalances={result['n_rebalances']}"
        )
    if not result["day_index"]:
        replay_errors.append("no_holding_days")
    elif max(result["day_index"]) > open_event_date:
        replay_errors.append(f"returns_after_open_event={max(result['day_index'])}>{open_event_date}")
    min_rebalances = int(wf.get("lockbox_min_rebalances", 6))
    if int(result["n_rebalances"]) < min_rebalances:
        replay_errors.append(f"rebalances={result['n_rebalances']}<{min_rebalances}")
    if len(result["day_index"]) < int(wf.get("min_days", 250)):
        replay_errors.append(f"holding_days={len(result['day_index'])}<{int(wf.get('min_days', 250))}")
    for arm in arms:
        if len(result["net"].get(arm, [])) != len(result["day_index"]):
            replay_errors.append(f"{arm}:net_curve_length_mismatch")
    if replay_errors:
        LOGGER.error("Lockbox replay failed publication gates: %s", replay_errors)
        return 1
    arm_rows = summarize_arms(result, arms, verdict_cfg=wf)

    ledger_dir.parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".lockbox_ledger-", dir=str(ledger_dir.parent)))
    try:
        arm_path = temp_dir / "oos_arm_comparison.csv"
        curves_path = temp_dir / "oos_daily_curves.csv"
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
        dev_path = temp_dir / "dev_arm_comparison.csv"
        if dev_builds:
            shutil.copyfile(dev_builds[-1] / "arm_comparison.csv", dev_path)
            dev_copy = dev_builds[-1].name

        output_files = [arm_path, curves_path] + ([dev_path] if dev_path.exists() else [])
        content_chain = hashlib.sha256()
        for output_file in output_files:
            content_chain.update(output_file.name.encode("utf-8"))
            content_chain.update(sha256_file(output_file).encode("ascii"))
        files = {
            "oos_arm_comparison.csv": {"sha256": sha256_file(arm_path), "rows": len(arm_rows)},
            "oos_daily_curves.csv": {"sha256": sha256_file(curves_path), "rows": len(curve_rows)},
        }
        if dev_path.exists():
            files["dev_arm_comparison.csv"] = {"sha256": sha256_file(dev_path)}
        manifest = {
            "stage": "stage11_lockbox_ledger",
            "generated_at": utc_now(),
            "acceptance": "PASS",
            "append_only": True,
            "open_event_date": open_event_date,
            "protocol_sha256": lockbox["protocol_sha256"],
            "sealed_window": [min(sealed), max(sealed)],
            "sealed_snapshots": {
                d: {
                    "stocks_scores_sha256": sha256_file(p / "stocks_scores.csv"),
                    "snapshot_meta_sha256": sha256_file(p / "snapshot_meta.json"),
                    "source_manifest_sha256": sha256_file(p / "manifest.json"),
                } for d, p in sealed.items()
            },
            "panel_build": panel_dir.name,
            "panel_manifest_sha256": sha256_file(panel_manifest_path),
            "panel_prices_sha256": sha256_file(prices_path),
            "panel_effective_end": str(prices.index[-1]),
            "macro_serving_sha256": macro_db_hash_after,
            "dev_comparison_build": dev_copy,
            "params": params,
            "arms": arms,
            "n_rebalances": result["n_rebalances"],
            "holding_days": len(result["day_index"]),
            "content_chain_sha256": content_chain.hexdigest(),
            "checks": [
                {
                    "check": "pit_no_violations",
                    "status": "PASS",
                    "detail": (
                        "independently checked covariance<=signal<execution for "
                        f"{pit_checked} executable rebalances"
                    ),
                },
                {"check": "open_event_right_edge", "status": "PASS",
                 "detail": f"last_return={max(result['day_index'])}<=open_event={open_event_date}"},
                {"check": "minimum_evidence", "status": "PASS",
                 "detail": f"rebalances={result['n_rebalances']} days={len(result['day_index'])}"},
            ],
            "inputs_sha256": {
                "config.yaml": sha256_file(config_path),
                "backtest/17_publish_lockbox_ledger.py": sha256_file(Path(__file__).resolve()),
                "backtest/walkforward_common.py": sha256_file(
                    Path(__file__).with_name("walkforward_common.py")
                ),
                "research/stage11_common.py": sha256_file(
                    PACKAGE_ROOT / "research" / "stage11_common.py"
                ),
                "optimizer/optimizer_core.py": sha256_file(
                    PACKAGE_ROOT / "optimizer" / "optimizer_core.py"
                ),
                "survivorship_manifest.json": sha256_file(panel_manifest_path),
                "prices_adjclose.csv": sha256_file(prices_path),
                "macro_serving.sqlite": macro_db_hash_after,
            },
            "files": files,
        }
        write_manifest(temp_dir / "lockbox_ledger_manifest.json", manifest)
        os.replace(temp_dir, ledger_dir)
    except Exception as exc:  # noqa: BLE001 - one-time publication must clean up and fail closed.
        shutil.rmtree(temp_dir, ignore_errors=True)
        LOGGER.exception("Atomic lockbox publication failed: %s", exc)
        return 1
    for r in arm_rows:
        LOGGER.info("OOS ARM %-10s net_sharpe=%s net_ir=%s active_t=%s promotable=%s",
                    r["arm"], r["net_sharpe"], r["net_ir_vs_baseline"], r["active_t"], r["promotable"])
    LOGGER.info("LOCKBOX LEDGER PUBLISHED (once): %s", ledger_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
