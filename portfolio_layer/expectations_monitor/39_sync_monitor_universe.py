#!/usr/bin/env python3
"""Build and seal the expectations-monitor universe from accepted portfolio artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    manifest_acceptance_value,
    read_csv,
    read_manifest,
    sealed_artifact_errors,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    UNIVERSE_FIELDS,
    build_universe_rows,
    connect_monitor_db,
    fetch_universe_snapshot,
    replace_universe_snapshot,
    utc_now,
    writer_lock,
)
from portfolio_layer.ledger.ledger_common import (  # noqa: E402
    latest_sealed_ledger_run,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
VALIDATION_FIELDS = ["check", "status", "detail"]
DEFERRED_LEDGER_POLICY = (
    "use_latest_sealed_ledger_and_defer_current_broker_groups"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--pending-orders-csv", type=Path)
    parser.add_argument("--pending-orders-manifest", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _sealed_source(
    *,
    role: str,
    artifact: Path,
    manifest_path: Path,
    artifact_keys: tuple[str, ...],
    run_as_of: str,
    required: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    if not artifact.exists() or not manifest_path.exists():
        if required:
            missing = [str(path) for path in (artifact, manifest_path) if not path.exists()]
            raise FileNotFoundError(f"Required monitor source {role} missing: {missing}")
        return [], []
    manifest = read_manifest(manifest_path)
    errors = sealed_artifact_errors(
        manifest,
        artifact,
        *artifact_keys,
        run_as_of=run_as_of,
        allow_deferred=True,
    )
    if errors:
        raise ValueError(f"Monitor source {role} is not sealed/current: {errors}")
    source = {
        "run_as_of": run_as_of,
        "source_role": role,
        "artifact_path": str(artifact),
        "artifact_sha256": sha256_file(artifact),
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "acceptance": manifest_acceptance_value(manifest),
    }
    return read_csv(artifact), [source]


def _broker_holdings_source(
    *,
    config: dict[str, Any],
    run_dir: Path,
    run_as_of: str,
    required: bool,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, Any]]:
    artifact = run_dir / "ledger" / "broker_net_stock_positions.csv"
    manifest_path = run_dir / "ledger" / "ledger_manifest.json"
    if artifact.exists() or manifest_path.exists():
        rows, sources = _sealed_source(
            role="broker_holdings",
            artifact=artifact,
            manifest_path=manifest_path,
            artifact_keys=(
                "broker_net_stock_positions",
                "broker_net_stock_positions.csv",
            ),
            run_as_of=run_as_of,
            required=required,
        )
        return rows, sources, {
            "status": "CURRENT" if sources else "NOT_REQUIRED",
            "source_as_of": run_as_of if sources else "",
            "age_days": 0 if sources else None,
            "policy": "same_date",
            "skipped_newer": [],
        }
    if not required:
        return [], [], {
            "status": "NOT_REQUIRED",
            "source_as_of": "",
            "age_days": None,
            "policy": "not_required",
            "skipped_newer": [],
        }

    policy = str(
        cfg_get(
            config,
            "holdings_ledger.missing_same_date_statement_policy",
            "fail",
        )
    ).strip()
    if policy != DEFERRED_LEDGER_POLICY:
        raise FileNotFoundError(
            f"Required same-date broker holdings are missing for {run_as_of}; "
            f"missing_same_date_statement_policy={policy or 'MISSING'}"
        )
    raw_staleness = cfg_get(config, "holdings_ledger.max_staleness_days", 7)
    try:
        max_staleness_days = int(str(raw_staleness))
    except ValueError as exc:
        raise ValueError(
            "holdings_ledger.max_staleness_days must be an integer, "
            f"got {raw_staleness!r}"
        ) from exc
    ledger_run, age_days, skipped = latest_sealed_ledger_run(
        run_dir.parent,
        run_as_of,
        max_staleness_days=max_staleness_days,
    )
    ledger_as_of = ledger_run.name
    rows, sources = _sealed_source(
        role="broker_holdings",
        artifact=ledger_run / "ledger" / "broker_net_stock_positions.csv",
        manifest_path=ledger_run / "ledger" / "ledger_manifest.json",
        artifact_keys=(
            "broker_net_stock_positions",
            "broker_net_stock_positions.csv",
        ),
        run_as_of=ledger_as_of,
        required=True,
    )
    for source in sources:
        source["consumer_run_as_of"] = run_as_of
        source["source_run_as_of"] = ledger_as_of
        source["source_age_days"] = str(age_days)
    return rows, sources, {
        "status": "DEFERRED_SAME_DATE",
        "source_as_of": ledger_as_of,
        "age_days": age_days,
        "policy": policy,
        "skipped_newer": skipped,
    }


def _row_digest(rows: list[dict[str, Any]]) -> str:
    import hashlib

    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_rows(
    rows: list[dict[str, Any]],
    *,
    score_rows: list[dict[str, str]],
    target_rows: list[dict[str, str]],
    holding_rows: list[dict[str, str]],
    pending_order_rows: list[dict[str, str]],
    source_count: int,
    expected_source_count: int,
    pending_orders_required: bool,
    pending_orders_integrated: bool,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def rec(check: str, passed: bool, detail: str) -> None:
        checks.append({"check": check, "status": "PASS" if passed else "FAIL", "detail": detail})

    tickers = [str(row["ticker"]) for row in rows]
    rec(
        "source_artifacts_sealed",
        source_count == expected_source_count,
        f"sealed_sources={source_count}; expected={expected_source_count}",
    )
    rec("universe_unique", len(tickers) == len(set(tickers)), f"rows={len(rows)}")
    rec("cash_excluded", "CASH" not in set(tickers), "CASH is not a monitored security")

    score_tickers = {str(row.get("ticker", "")).strip().upper() for row in score_rows}
    score_tickers.discard("")
    target_tickers = {
        str(row.get("ticker", "")).strip().upper()
        for row in target_rows
        if str(row.get("ticker", "")).strip().upper() != "CASH" and float(row.get("weight", 0.0)) > 1e-12
    }
    holding_tickers = {
        str(row.get("symbol", row.get("ticker", ""))).strip().upper()
        for row in holding_rows
        if abs(float(row.get("net_shares", row.get("quantity", 0.0)))) > 1e-12
    }
    pending_order_tickers = {
        str(row.get("ticker", row.get("symbol", ""))).strip().upper()
        for row in pending_order_rows
        if float(row.get("remaining_quantity", 0.0)) > 1e-12
    }
    pending_order_tickers.discard("")
    expected_union = (
        score_tickers | target_tickers | holding_tickers | pending_order_tickers
    )
    rec(
        "complete_union",
        set(tickers) == expected_union,
        f"actual={len(tickers)}; expected={len(expected_union)}",
    )

    tier0 = {str(row["ticker"]) for row in rows if row["tier"] == "tier0"}
    tier1 = {str(row["ticker"]) for row in rows if row["tier"] == "tier1"}
    tier2 = {str(row["ticker"]) for row in rows if row["tier"] == "tier2"}
    rec(
        "tier0_complete",
        target_tickers | holding_tickers | pending_order_tickers <= tier0,
        f"tier0={len(tier0)}; holdings={len(holding_tickers)}; "
        f"targets={len(target_tickers)}; pending_orders={len(pending_order_tickers)}",
    )
    expected_tier1 = {
        str(row["ticker"])
        for row in rows
        if not row["is_holding"] and not row["is_target"] and row["investable_eligible"]
    }
    rec("tier1_investable", tier1 == expected_tier1, f"tier1={len(tier1)}")
    rec(
        "partition_complete_disjoint",
        not (tier0 & tier1 or tier0 & tier2 or tier1 & tier2) and tier0 | tier1 | tier2 == set(tickers),
        f"tier0={len(tier0)}; tier1={len(tier1)}; tier2={len(tier2)}",
    )
    actual_pending = {
        str(row["ticker"])
        for row in rows
        if int(row["is_pending_order"]) == 1
    }
    if pending_orders_required or pending_orders_integrated:
        rec(
            "pending_orders_integrated",
            pending_orders_integrated and actual_pending == pending_order_tickers,
            f"source_rows={len(pending_order_rows)}; pending_tickers="
            f"{len(pending_order_tickers)}; stamped={len(actual_pending)}",
        )
    else:
        checks.append(
            {
                "check": "pending_orders_integrated",
                "status": "WARN",
                "detail": "not required by config and no sealed pending-order source supplied",
            }
        )
    return checks


def run_selftest() -> None:
    rows = build_universe_rows(
        run_as_of="2026-07-24",
        score_rows=[
            {
                "ticker": "AAA",
                "investable_eligible": "1",
                "final_score": "0.1",
                "score_confidence": "0.9",
            },
            {
                "ticker": "BBB",
                "investable_eligible": "0",
                "final_score": "-0.1",
                "score_confidence": "0.5",
            },
        ],
        target_rows=[{"ticker": "BBB", "weight": "0.2"}, {"ticker": "CASH", "weight": "0.8"}],
        holding_rows=[{"symbol": "CCC", "net_shares": "10"}],
        updated_at_utc="2026-07-24T23:59:59+00:00",
        pending_order_tickers=[],
    )
    checks = validate_rows(
        rows,
        score_rows=[{"ticker": "AAA"}, {"ticker": "BBB"}],
        target_rows=[{"ticker": "BBB", "weight": "0.2"}, {"ticker": "CASH", "weight": "0.8"}],
        holding_rows=[{"symbol": "CCC", "net_shares": "10"}],
        pending_order_rows=[],
        source_count=3,
        expected_source_count=3,
        pending_orders_required=False,
        pending_orders_integrated=False,
    )
    assert not [row for row in checks if row["status"] == "FAIL"]
    assert {row["ticker"]: row["tier"] for row in rows} == {
        "AAA": "tier1",
        "BBB": "tier0",
        "CCC": "tier0",
    }
    pending_rows = build_universe_rows(
        run_as_of="2026-07-24",
        score_rows=[{"ticker": "AAA", "investable_eligible": "1"}],
        target_rows=[],
        holding_rows=[],
        pending_order_tickers=["DDD"],
        updated_at_utc="2026-07-24T23:59:59+00:00",
    )
    pending_checks = validate_rows(
        pending_rows,
        score_rows=[{"ticker": "AAA"}],
        target_rows=[],
        holding_rows=[],
        pending_order_rows=[{"ticker": "DDD", "remaining_quantity": "5"}],
        source_count=4,
        expected_source_count=4,
        pending_orders_required=True,
        pending_orders_integrated=True,
    )
    assert not [row for row in pending_checks if row["status"] == "FAIL"]
    assert {row["ticker"]: row["tier"] for row in pending_rows} == {
        "AAA": "tier1",
        "DDD": "tier0",
    }
    assert next(
        row for row in pending_rows if row["ticker"] == "DDD"
    )["is_pending_order"] == 1
    print("monitor universe synchronizer selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")

    config_path = args.config.resolve()
    config = load_yaml(config_path)
    paths = resolve_runtime_paths(config, config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    if not isinstance(monitor_cfg, dict):
        raise ValueError("expectations_monitor config must be a mapping")
    run_as_of = args.as_of.isoformat()
    run_dir = paths.output_dir / "runs" / run_as_of
    universe_cfg = monitor_cfg.get("universe", {})
    if not isinstance(universe_cfg, dict):
        raise ValueError("expectations_monitor.universe must be a mapping")

    score_rows, score_sources = _sealed_source(
        role="scores",
        artifact=run_dir / "stocks_scores.csv",
        manifest_path=run_dir / "manifest.json",
        artifact_keys=("stocks_scores.csv",),
        run_as_of=run_as_of,
        required=bool(universe_cfg.get("require_scores", True)),
    )
    bootstrap_target = run_dir / "final" / "bootstrap_target_weights.csv"
    bootstrap_manifest = (
        run_dir / "final" / "bootstrap_final_weights_manifest.json"
    )
    target_artifact = (
        bootstrap_target
        if bootstrap_target.is_file() and bootstrap_manifest.is_file()
        else run_dir / "final" / "final_target_weights.csv"
    )
    target_manifest = (
        bootstrap_manifest
        if target_artifact == bootstrap_target
        else run_dir / "final" / "final_weights_manifest.json"
    )
    target_key = (
        "bootstrap_target_weights.csv"
        if target_artifact == bootstrap_target
        else "final_target_weights.csv"
    )
    target_rows, target_sources = _sealed_source(
        role="final_target",
        artifact=target_artifact,
        manifest_path=target_manifest,
        artifact_keys=(target_key,),
        run_as_of=run_as_of,
        required=bool(universe_cfg.get("require_final_target", True)),
    )
    holding_rows, holding_sources, holdings_dependency = _broker_holdings_source(
        config=config,
        run_dir=run_dir,
        run_as_of=run_as_of,
        required=bool(universe_cfg.get("require_broker_holdings", True)),
    )
    pending_orders_required = bool(universe_cfg.get("require_pending_orders", False))
    if (args.pending_orders_csv is None) != (args.pending_orders_manifest is None):
        raise ValueError(
            "--pending-orders-csv and --pending-orders-manifest must be supplied together"
        )
    pending_orders_integrated = args.pending_orders_csv is not None
    pending_rows: list[dict[str, str]] = []
    pending_sources: list[dict[str, str]] = []
    if pending_orders_integrated or pending_orders_required:
        output_subdir = str(
            monitor_cfg.get("output_subdir", "expectations_monitor")
        ).strip()
        pending_csv = (
            args.pending_orders_csv.resolve()
            if args.pending_orders_csv is not None
            else run_dir / output_subdir / "pending_orders" / "ib_pending_orders.csv"
        )
        pending_manifest = (
            args.pending_orders_manifest.resolve()
            if args.pending_orders_manifest is not None
            else run_dir
            / output_subdir
            / "pending_orders"
            / "ib_pending_orders_manifest.json"
        )
        pending_rows, pending_sources = _sealed_source(
            role="pending_orders",
            artifact=pending_csv,
            manifest_path=pending_manifest,
            artifact_keys=("ib_pending_orders.csv",),
            run_as_of=run_as_of,
            required=pending_orders_required or pending_orders_integrated,
        )
        pending_orders_integrated = bool(pending_sources)
    sources = score_sources + target_sources + holding_sources + pending_sources
    expected_sources = sum(
        int(bool(universe_cfg.get(key, True)))
        for key in ("require_scores", "require_final_target", "require_broker_holdings")
    )
    expected_sources += int(pending_orders_required or pending_orders_integrated)
    pending_tickers = {
        str(row.get("ticker", row.get("symbol", ""))).strip().upper()
        for row in pending_rows
        if float(row.get("remaining_quantity", 0.0)) > 1e-12
    }
    pending_tickers.discard("")
    rows = build_universe_rows(
        run_as_of=run_as_of,
        score_rows=score_rows,
        target_rows=target_rows,
        holding_rows=holding_rows,
        updated_at_utc=f"{run_as_of}T23:59:59+00:00",
        pending_order_tickers=pending_tickers,
    )
    checks = validate_rows(
        rows,
        score_rows=score_rows,
        target_rows=target_rows,
        holding_rows=holding_rows,
        pending_order_rows=pending_rows,
        source_count=len(sources),
        expected_source_count=expected_sources,
        pending_orders_required=pending_orders_required,
        pending_orders_integrated=pending_orders_integrated,
    )
    checks.append(
        {
            "check": "broker_holdings_current_or_bounded_fallback",
            "status": "PASS",
            "detail": (
                f"status={holdings_dependency['status']}; "
                f"source_as_of={holdings_dependency['source_as_of'] or 'none'}; "
                f"age_days={holdings_dependency['age_days']}; "
                f"policy={holdings_dependency['policy']}"
            ),
        }
    )
    failures = [row for row in checks if row["status"] == "FAIL"]
    if failures:
        raise RuntimeError(f"Monitor universe validation failed: {failures}")

    output_subdir = str(monitor_cfg.get("output_subdir", "expectations_monitor")).strip()
    if not output_subdir or Path(output_subdir).is_absolute() or ".." in Path(output_subdir).parts:
        raise ValueError("expectations_monitor.output_subdir must be a safe relative path")
    output_dir = run_dir / output_subdir
    universe_path = output_dir / "monitor_universe.csv"
    validation_path = output_dir / "monitor_universe_validation.csv"
    meta_path = output_dir / "monitor_universe_meta.json"
    manifest_path = output_dir / "monitor_universe_manifest.json"
    fail_if_exists([universe_path, validation_path, meta_path, manifest_path], force=args.force)

    write_csv(universe_path, UNIVERSE_FIELDS, rows)
    write_csv(validation_path, VALIDATION_FIELDS, checks)
    counts = {tier: sum(row["tier"] == tier for row in rows) for tier in ("tier0", "tier1", "tier2")}
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    meta = {
        "schema_version": "monitor_universe_meta_v1",
        "acceptance": "PASS",
        "run_as_of": run_as_of,
        "generated_at_utc": generated_at,
        "row_count": len(rows),
        "tier_counts": counts,
        "row_digest": _row_digest(rows),
        "source_artifacts": sources,
        "broker_holdings_dependency": holdings_dependency,
        "shadow_only": True,
        "pending_orders_integrated": pending_orders_integrated,
    }
    write_manifest(meta_path, meta)

    db_path = ensure_not_prod_path(
        args.db.resolve()
        if args.db
        else resolve_path(
            monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"),
            base_dir=config_path.parent,
        ),
        label="expectations monitor database",
    )
    lock_path = db_path.with_suffix(db_path.suffix + ".writer.lock")
    timeout_sec = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    with writer_lock(lock_path, timeout_sec=timeout_sec):
        conn = connect_monitor_db(db_path, timeout_sec=timeout_sec)
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO monitor_runs(run_as_of, started_at_utc, status) VALUES (?, ?, ?)",
                    (run_as_of, utc_now(), "RUNNING"),
                )
                run_id = cursor.lastrowid
            try:
                replace_universe_snapshot(
                    conn,
                    run_as_of=run_as_of,
                    rows=rows,
                    source_artifacts=sources,
                )
                db_rows = fetch_universe_snapshot(conn, run_as_of)
                if db_rows != rows:
                    raise RuntimeError("SQLite universe snapshot differs from sealed CSV rows")
            except Exception as exc:
                with conn:
                    conn.execute(
                        "UPDATE monitor_runs SET completed_at_utc=?, status='FAIL', message=? WHERE run_id=?",
                        (utc_now(), str(exc), run_id),
                    )
                raise
            with conn:
                conn.execute(
                    "UPDATE monitor_runs SET completed_at_utc=?, status='PASS', row_count=? WHERE run_id=?",
                    (utc_now(), len(rows), run_id),
                )
        finally:
            conn.close()

    input_paths = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("monitor_common.py").resolve(),
        *[Path(source["artifact_path"]) for source in sources],
        *[Path(source["manifest_path"]) for source in sources],
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "monitor_universe_manifest_v1",
            "acceptance": "PASS",
            "run_as_of": run_as_of,
            "generated_at_utc": generated_at,
            "shadow_only": True,
            "database_path": str(db_path),
            "broker_holdings_dependency": holdings_dependency,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                universe_path.name: sha256_file(universe_path),
                validation_path.name: sha256_file(validation_path),
                meta_path.name: sha256_file(meta_path),
            },
        },
    )
    print("MONITOR UNIVERSE: PASS")
    print(f"rows: {len(rows)}; tiers: {counts}")
    print(f"output: {universe_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
