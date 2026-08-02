#!/usr/bin/env python3
"""Independently validate and seal the expectations-state contract."""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_csv,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    ACTION_STATES,
    INTERNAL_STATES,
    ensure_state_schema,
    trading_days_between,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def validate_rows(
    rows: list[dict[str, str]],
    db_rows: list[dict[str, Any]],
    expected_tickers: set[str] | None = None,
) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def rec(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    tickers = [row["ticker"] for row in rows]
    rec("state_rows_present_unique", bool(rows) and len(tickers) == len(set(tickers)), f"rows={len(rows)}")
    if expected_tickers is not None:
        rec(
            "universe_complete",
            set(tickers) == expected_tickers,
            f"state={len(set(tickers))}; universe={len(expected_tickers)}",
        )
    rec(
        "closed_state_contract",
        all(row["internal_state"] in INTERNAL_STATES and row["action_state"] in ACTION_STATES for row in rows),
        "all internal/action states are enumerated",
    )
    finite = all(
        all(math.isfinite(float(row[field])) for field in ("baseline_points", "company_event_points", "external_intel_points", "market_points", "peer_readthrough_points", "les_total"))
        for row in rows
    )
    rec("components_finite", finite, "all LES components finite")
    sums_match = all(
        abs(
            float(row["les_total"])
            - max(
                -100.0,
                min(
                    100.0,
                    sum(float(row[field]) for field in ("baseline_points", "company_event_points", "external_intel_points", "market_points", "peer_readthrough_points")),
                ),
            )
        ) <= 1e-8
        for row in rows
    )
    rec("component_sum_recomputed", sums_match, "LES equals clipped visible-component sum")
    rec(
        "market_component_bounded",
        all(abs(float(row["market_points"])) <= 15.0 + 1e-9 for row in rows),
        "market component respects +/-15 cap",
    )
    rec(
        "market_data_status_closed",
        all(
            row.get("market_data_status") in {"current", "missing_latest"}
            for row in rows
        ),
        "market status is current or missing_latest",
    )
    missing_market_safe = all(
        row.get("market_data_status") != "missing_latest"
        or (
            float(row["market_points"]) == 0.0
            and "MARKET_DATA_UNAVAILABLE"
            in set(json.loads(row["escalation_flags_json"]))
            and row["action_state"]
            in {"watch", "suspend_adds", "deteriorating", "exit_review"}
        )
        for row in rows
    )
    rec(
        "missing_market_data_fail_closed_per_name",
        missing_market_safe,
        "missing names have zero market points and cannot buy/add/hold",
    )
    rec(
        "action_eligibility_fail_closed",
        all(
            row["action_state"] not in {"buy_candidate", "add_candidate"}
            or int(float(row["investable_eligible"])) == 1
            for row in rows
        ),
        "buy/add states require investable eligibility",
    )
    rec(
        "state_layer_never_authorizes_entry",
        all(row["action_state"] not in {"buy_candidate", "add_candidate"} for row in rows),
        "buy/add authorization belongs exclusively to the validated levels engine",
    )
    rec(
        "exit_review_human_contract",
        all(row["action_state"] != "exit_review" or row["internal_state"] == "broken" for row in rows),
        "exit_review is recommendation-only and requires broken internal state",
    )
    csv_identity = [
        (
            row["ticker"], row["internal_state"], row["action_state"],
            row["market_data_status"], row["input_digest"],
        )
        for row in rows
    ]
    db_identity = [
        (
            str(row["ticker"]), str(row["internal_state"]), str(row["action_state"]),
            str(row["market_data_status"]), str(row["input_digest"]),
        )
        for row in db_rows
    ]
    rec("sqlite_csv_exact", csv_identity == db_identity, f"csv={len(csv_identity)}; db={len(db_identity)}")
    return checks


def run_selftest() -> None:
    row = {
        "ticker": "AAA", "internal_state": "green", "action_state": "watch",
        "investable_eligible": "1", "baseline_points": "10", "company_event_points": "0",
        "external_intel_points": "0", "market_points": "0", "peer_readthrough_points": "0",
        "les_total": "10", "market_data_status": "current",
        "escalation_flags_json": "[]", "input_digest": "x",
    }
    assert not [check for check in validate_rows([row], [row]) if check["status"] == "FAIL"]
    row["action_state"] = "buy_candidate"
    row["investable_eligible"] = "0"
    assert any(check["check"] == "action_eligibility_fail_closed" and check["status"] == "FAIL" for check in validate_rows([row], [row]))
    row.update(
        {
            "action_state": "suspend_adds",
            "market_data_status": "missing_latest",
            "market_points": "0",
            "escalation_flags_json": '["MARKET_DATA_UNAVAILABLE"]',
        }
    )
    assert not [
        check
        for check in validate_rows([row], [row])
        if check["check"] == "missing_market_data_fail_closed_per_name"
        and check["status"] == "FAIL"
    ]
    print("expectations state validator selftest: PASS")


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
    input_dir = (
        args.input_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
    )
    state_path = input_dir / "expectations_state.csv"
    state_manifest_path = input_dir / "expectations_state_manifest.json"
    manifest = read_manifest(state_manifest_path)
    if manifest.get("acceptance") != "PASS" or manifest.get("as_of_date") != args.as_of.isoformat():
        raise ValueError("Expectations state manifest is not accepted/current")
    expected = dict(manifest.get("outputs_sha256", {})).get(state_path.name)
    if not state_path.is_file() or expected != sha256_file(state_path):
        raise ValueError("Expectations state CSV hash mismatch")
    for source, expected_sha in dict(manifest.get("inputs_sha256", {})).items():
        path = Path(source)
        if not path.is_file() or sha256_file(path) != expected_sha:
            raise ValueError(f"Expectations state input drift: {path}")
    rows = read_csv(state_path)
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    conn = connect_monitor_db(db_path, timeout_sec=float(monitor_cfg.get("writer_lock_timeout_sec", 30.0)))
    try:
        ensure_state_schema(conn)
        db_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT ticker,internal_state,action_state,market_data_status,input_digest FROM les_snapshots WHERE run_as_of=? ORDER BY ticker",
                (args.as_of.isoformat(),),
            ).fetchall()
        ]
        universe_as_of = str(manifest.get("universe_as_of", args.as_of.isoformat()))
        expected_tickers = {
            str(row["ticker"])
            for row in conn.execute(
                "SELECT ticker FROM monitor_universe WHERE run_as_of=?", (universe_as_of,)
            ).fetchall()
        }
        broken = [row["ticker"] for row in rows if row["internal_state"] == "broken"]
        unsupported: list[str] = []
        for ticker in broken:
            confirmed = conn.execute(
                """
                SELECT COUNT(*) FROM events
                WHERE ticker=? AND event_date<=?
                  AND thesis_break_flag=1 AND review_status='confirmed'
                """,
                (ticker, args.as_of.isoformat()),
            ).fetchone()[0]
            cuts = [
                str(row["event_date"])
                for row in conn.execute(
                    """
                    SELECT event_date FROM events
                    WHERE ticker=? AND event_type='guidance_cut'
                      AND review_status!='dismissed' AND event_date<=?
                    """,
                    (ticker, args.as_of.isoformat()),
                ).fetchall()
                if trading_days_between(str(row["event_date"]), args.as_of.isoformat()) <= 130
            ]
            if int(confirmed) < 1 and len(set(cuts)) < 2:
                unsupported.append(str(ticker))
    finally:
        conn.close()
    checks = validate_rows(rows, db_rows, expected_tickers)
    checks.append(
        {
            "check": "broken_requires_hard_evidence",
            "status": "PASS" if not unsupported else "FAIL",
            "detail": f"broken={len(broken)}; unsupported={unsupported}",
        }
    )
    failures = [check for check in checks if check["status"] == "FAIL"]
    validation_dir = input_dir / "validation"
    checks_path = validation_dir / "expectations_state_validation.csv"
    validation_manifest_path = validation_dir / "expectations_state_validation_manifest.json"
    fail_if_exists([checks_path, validation_manifest_path], force=args.force)
    write_csv(checks_path, VALIDATION_FIELDS, checks)
    acceptance = "FAIL" if failures else "PASS"
    input_paths = [config_path, Path(__file__).resolve(), Path(__file__).with_name("state_common.py"), state_manifest_path]
    write_manifest(
        validation_manifest_path,
        {
            "schema_version": "expectations_state_validation_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "row_count": len(rows),
            "broker_execution_prohibited": True,
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {checks_path.name: sha256_file(checks_path)},
        },
    )
    print(f"EXPECTATIONS STATE VALIDATION: {acceptance}")
    print(f"checks={len(checks)}; manifest={validation_manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
