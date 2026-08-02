#!/usr/bin/env python3
"""Classify structured raw items deterministically and seal the event surface."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_layer.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from portfolio_layer.core.contracts import (  # noqa: E402
    fail_if_exists,
    read_manifest,
    sha256_file,
    write_csv,
    write_manifest,
)
from portfolio_layer.core.paths import ensure_not_prod_path, resolve_runtime_paths  # noqa: E402
from portfolio_layer.expectations_monitor.monitor_common import (  # noqa: E402
    connect_monitor_db,
    database_writer_lock,
    monitor_output_subdir,
)
from portfolio_layer.expectations_monitor.state_common import (  # noqa: E402
    ACTION_STATES,
    CLASSIFIER_VERSION,
    EVENT_SPECS,
    INTERNAL_STATES,
    append_classified_events,
    ensure_state_schema,
)


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
EVENT_FIELDS = [
    "event_id", "event_key", "ticker", "event_type", "category", "event_date",
    "detected_at_utc", "direction", "severity", "credibility", "novelty", "relevance",
    "impact_0", "half_life_td", "decay_mode", "driver_tag", "origin_ticker",
    "source_item_ids", "classifier", "classifier_version", "rationale_text",
    "material_flag", "thesis_break_flag", "review_status",
]
VALIDATION_FIELDS = ["check", "status", "detail"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    return parser.parse_args()


def _validate(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    checks: list[dict[str, str]] = []

    def rec(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "status": "PASS" if passed else "FAIL", "detail": detail})

    rec("event_identity_unique", len(rows) == len({row["event_id"] for row in rows}), f"rows={len(rows)}")
    rec(
        "taxonomy_closed",
        all(str(row["event_type"]) in EVENT_SPECS for row in rows),
        f"taxonomy_version_rows={len(rows)}",
    )
    rec(
        "probability_bounds",
        all(
            -1.0 <= float(row["direction"]) <= 1.0
            and 0.0 <= float(row["credibility"]) <= 1.0
            and 0.0 <= float(row["novelty"]) <= 1.0
            and 0.0 <= float(row["relevance"]) <= 1.0
            for row in rows
        ),
        "direction, credibility, novelty, and relevance bounded",
    )
    rec(
        "classifier_deterministic_rules_only",
        all(row["classifier"] == "rule" and row["classifier_version"] == CLASSIFIER_VERSION for row in rows),
        "no LLM or provider sentiment classification accepted",
    )
    rec(
        "thesis_break_fail_closed",
        all(
            not int(row["thesis_break_flag"])
            or row["event_type"] in {
                "accounting_restatement_or_material_weakness",
                "balance_sheet_distress",
            }
            for row in rows
        ),
        "automatic thesis breaks limited to taxonomy-always types",
    )
    return checks


def _rows_for_current_raw_items(
    rows: list[dict[str, Any]], raw_item_ids: set[str]
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in rows:
        try:
            source_ids = json.loads(str(row["source_item_ids"]))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(
                f"Invalid source_item_ids for event {row.get('event_id', '')}"
            ) from exc
        if not isinstance(source_ids, list) or not all(
            isinstance(value, str) and value for value in source_ids
        ):
            raise ValueError(
                f"Invalid source_item_ids contract for event {row.get('event_id', '')}"
            )
        if raw_item_ids.intersection(source_ids):
            selected.append(row)
    return selected


def run_selftest() -> None:
    assert set(INTERNAL_STATES) == {"green", "stable", "watch", "deteriorating", "broken"}
    assert set(ACTION_STATES) == {
        "buy_candidate", "add_candidate", "hold", "watch", "deteriorating", "suspend_adds", "exit_review"
    }
    assert not [row for row in _validate([]) if row["status"] == "FAIL"]
    rows = [
        {"event_id": "kept", "source_item_ids": '["current"]'},
        {"event_id": "stale", "source_item_ids": '["old"]'},
    ]
    assert [
        row["event_id"] for row in _rows_for_current_raw_items(rows, {"current"})
    ] == ["kept"]
    print("monitor event classification selftest: PASS")


def main() -> int:
    args = parse_args()
    if args.selftest:
        run_selftest()
        return 0
    if args.as_of is None:
        raise ValueError("--as-of is required")
    config_path = args.config.resolve()
    config = load_yaml(config_path)
    monitor_cfg = cfg_get(config, "expectations_monitor", {})
    event_cfg = cfg_get(config, "expectations_monitor.events", {})
    reconciliation_cfg = cfg_get(
        config, "expectations_monitor.provider_reconciliation", {}
    )
    if (
        not isinstance(monitor_cfg, dict)
        or not isinstance(event_cfg, dict)
        or not isinstance(reconciliation_cfg, dict)
    ):
        raise ValueError(
            "expectations_monitor events and provider_reconciliation must be mappings"
        )
    db_path = ensure_not_prod_path(
        resolve_path(monitor_cfg.get("database_path", "db/expectations_monitor.sqlite"), base_dir=config_path.parent),
        label="expectations monitor database",
    )
    paths = resolve_runtime_paths(config, config_path)
    input_dir = (
        args.input_dir
        or paths.output_dir
        / "runs"
        / args.as_of.isoformat()
        / monitor_output_subdir(config)
        / "events"
    )
    ingestion_manifest_path = input_dir / "event_ingestion_manifest.json"
    if not ingestion_manifest_path.is_file():
        raise FileNotFoundError(ingestion_manifest_path)
    ingestion_manifest = read_manifest(ingestion_manifest_path)
    ingestion_acceptance = str(ingestion_manifest.get("acceptance", ""))
    if (
        ingestion_acceptance not in {"PASS", "PASS_WITH_DEFERRED"}
        or ingestion_manifest.get("as_of_date") != args.as_of.isoformat()
    ):
        raise ValueError("Event ingestion manifest is not accepted/current")
    for filename, expected in dict(ingestion_manifest.get("outputs_sha256", {})).items():
        path = input_dir / filename
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"Event ingestion output is not sealed: {filename}")
    raw_items_path = input_dir / "raw_items.csv"
    with raw_items_path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_item_ids = {
            str(row.get("item_id", "")).strip() for row in csv.DictReader(handle)
        }
    if "" in raw_item_ids:
        raise ValueError("Current raw-item surface contains a blank item_id")
    output_dir = args.output_dir or input_dir
    events_path = output_dir / "events.csv"
    checks_path = output_dir / "event_classification_validation.csv"
    manifest_path = output_dir / "event_classification_manifest.json"
    fail_if_exists([events_path, checks_path, manifest_path], force=args.force)
    timeout = float(monitor_cfg.get("writer_lock_timeout_sec", 30.0))
    state_cfg = monitor_cfg.get("state_model", {})
    if not isinstance(state_cfg, dict):
        raise ValueError("expectations_monitor.state_model must be a mapping")
    lookback = int(event_cfg.get("lookback_calendar_days", 200))
    floor = (args.as_of - timedelta(days=lookback)).isoformat()
    active_period_grace_days = int(
        reconciliation_cfg.get("active_period_grace_days", 90)
    )
    if active_period_grace_days < 0:
        raise ValueError("active_period_grace_days must be non-negative")
    minimum_estimate_fiscal_period_end = args.as_of - timedelta(
        days=active_period_grace_days
    )
    conn = connect_monitor_db(db_path, timeout_sec=timeout)
    try:
        ensure_state_schema(conn)
        with database_writer_lock(db_path, timeout_sec=timeout):
            inserted, irrelevant = append_classified_events(
                conn,
                as_of=args.as_of.isoformat(),
                raw_item_ids=raw_item_ids,
                minimum_estimate_fiscal_period_end=(
                    minimum_estimate_fiscal_period_end
                ),
                novelty_repeat_window_trading_days=int(
                    state_cfg.get("novelty_repeat_window_trading_days", 20)
                ),
                novelty_repeat_value=float(
                    state_cfg.get("novelty_repeat_value", 0.30)
                ),
            )
        candidate_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM events WHERE event_date BETWEEN ? AND ? ORDER BY event_date,ticker,event_id",
                (floor, args.as_of.isoformat()),
            ).fetchall()
        ]
        rows = _rows_for_current_raw_items(candidate_rows, raw_item_ids)
    finally:
        conn.close()
    checks = _validate(rows)
    failures = [row for row in checks if row["status"] == "FAIL"]
    write_csv(events_path, EVENT_FIELDS, rows)
    write_csv(checks_path, VALIDATION_FIELDS, checks)
    acceptance = (
        "FAIL"
        if failures
        else "PASS_WITH_DEFERRED"
        if ingestion_acceptance == "PASS_WITH_DEFERRED"
        else "PASS"
    )
    input_paths = [
        config_path,
        Path(__file__).resolve(),
        Path(__file__).with_name("state_common.py"),
        ingestion_manifest_path,
    ]
    write_manifest(
        manifest_path,
        {
            "schema_version": "event_classification_manifest_v1",
            "acceptance": acceptance,
            "as_of_date": args.as_of.isoformat(),
            "event_count": len(rows),
            "inserted_event_count": len(inserted),
            "irrelevant_raw_item_count": irrelevant,
            "classifier_version": CLASSIFIER_VERSION,
            "provider_revision_active_period_grace_days": active_period_grace_days,
            "current_raw_item_count": len(raw_item_ids),
            "rules_only": bool(event_cfg.get("rules_only", True)),
            "policy_version": str(event_cfg.get("policy_version", "")),
            "inputs_sha256": {str(path): sha256_file(path) for path in input_paths},
            "outputs_sha256": {
                events_path.name: sha256_file(events_path),
                checks_path.name: sha256_file(checks_path),
            },
        },
    )
    print(f"EVENT CLASSIFICATION: {acceptance}")
    print(f"events={len(rows)}; inserted={len(inserted)}; manifest={manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
