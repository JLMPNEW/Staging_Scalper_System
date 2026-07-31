#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import (  # noqa: E402
    cfg_get,
    family_config,
    load_yaml,
    resolve_path,
)
from industrials.core.db import connect  # noqa: E402
from industrials.core.oos_research import (  # noqa: E402
    artifact_sha256,
    execution_window,
    fmt,
    load_adjusted_open_prices,
    parse_date,
    purged_split_map,
    select_weekly_dates,
)
from industrials.core.reports import (  # noqa: E402
    write_csv_atomic,
    write_text_atomic,
)
from industrials.core.score_history import (  # noqa: E402
    validate_shadow_survivorship_sidecar,
)
from industrials.transportation.contracts import (  # noqa: E402
    COMPONENT_FIELDS,
    read_rows,
)
from industrials.transportation.oos_identity import (  # noqa: E402
    load_aliases,
    load_continuity,
    load_memberships,
    resolve_price_ticker,
)
from industrials.transportation.scripts._shared import (  # noqa: E402
    DEFAULT_CONFIG,
    MODEL_FAMILY,
)


SIDECAR = "transportation_stage11_survivorship_calibration_panel.csv"
RANK_MANIFEST = "transportation_final_rank_table_manifest.json"
HORIZONS = (21, 63)
PANEL_FIELDS = [
    "asof_date",
    "ticker",
    "company_name",
    "calibration_cohort",
    "calibration_use",
    "development_stage",
    "membership_source_id",
    "membership_start_date",
    "membership_end_date",
    "membership_status",
    "physical_price_ticker",
    "alias_resolution",
    *COMPONENT_FIELDS,
    "baseline_final_score",
    "rank_ready_flag",
    "stage11_calibration_input_eligible_flag",
    "calibration_eligible_flag",
    "calibration_eligible_reason",
    "horizon_sessions",
    "security_price_source_id",
    "entry_date",
    "entry_adjusted_open",
    "exit_date",
    "exit_execution_value",
    "outcome_method",
    "security_forward_return",
    "benchmark_ticker",
    "benchmark_price_source_id",
    "benchmark_entry_date",
    "benchmark_exit_date",
    "benchmark_forward_return",
    "forward_excess_return",
    "outcome_available_flag",
    "outcome_unavailable_reason",
    "split",
    "source_sidecar_sha256",
    "source_rank_manifest_sha256",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build transportation's generic-score weekly OOS panel from "
            "sealed daily survivorship-corrected score sidecars."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--allow-overwrite", action="store_true")
    return parser.parse_args()


def load_snapshot(
    snapshot_dir: Path,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    sidecar = snapshot_dir / SIDECAR
    manifest_path = snapshot_dir / RANK_MANIFEST
    if not sidecar.is_file() or not manifest_path.is_file():
        raise FileNotFoundError(
            f"Incomplete transportation snapshot: {snapshot_dir}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = read_rows(sidecar)
    asof = snapshot_dir.name
    issues = validate_shadow_survivorship_sidecar(
        rows,
        asof_date=asof,
    )
    if (
        manifest.get("acceptance") != "PASS"
        or manifest.get("model_family") != MODEL_FAMILY
        or manifest.get("asof_date") != asof
        or int(manifest.get("stage11_survivorship_calibration_panel_row_count") or -1)
        != len(rows)
        or str(
            manifest.get(
                "stage11_survivorship_calibration_panel_sha256"
            )
            or ""
        )
        != artifact_sha256(sidecar)
    ):
        issues.append("rank manifest/sidecar integrity mismatch")
    if issues:
        raise ValueError(
            f"{snapshot_dir}: " + "; ".join(issues[:20])
        )
    return rows, manifest


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    family = family_config(config, MODEL_FAMILY)
    universe = family["universe"]
    historical = family["historical_scores"]
    standards = cfg_get(
        config,
        "oos_calibration_standards.families.transportation",
        {},
    )
    if not isinstance(standards, dict):
        raise ValueError(
            "Transportation OOS calibration standards are not configured"
        )
    dashboard_root = resolve_path(
        historical["output_root"],
        base_dir=base_dir,
    )
    output_root = resolve_path(
        standards["research_output_root"],
        base_dir=base_dir,
    )
    panel_path = output_root / "transportation_generic_oos_panel.csv"
    split_path = output_root / "transportation_generic_oos_splits.csv"
    manifest_path = output_root / "transportation_generic_oos_panel_manifest.json"
    if (
        not args.allow_overwrite
        and any(path.exists() for path in (panel_path, split_path, manifest_path))
    ):
        raise FileExistsError(
            "Generic transportation OOS panel is sealed; use "
            "--allow-overwrite for an explicit rebuild"
        )
    start = args.start_date or str(standards["development_start_date"])
    end = args.end_date or str(standards["development_end_date"])
    available = [
        path.name
        for path in dashboard_root.iterdir()
        if path.is_dir()
        and len(path.name) == 10
        and start <= path.name <= end
        and (path / SIDECAR).is_file()
        and (path / RANK_MANIFEST).is_file()
    ]
    selected = select_weekly_dates(
        available,
        anchor=str(standards["weekly_anchor_date"]),
        selection=str(standards.get("weekly_selection") or "last"),
    )
    if not selected:
        raise ValueError("No transportation score snapshots selected")
    snapshots: dict[str, list[dict[str, str]]] = {}
    snapshot_manifests: dict[str, dict[str, Any]] = {}
    all_contract_tickers: set[str] = set()
    for asof in selected:
        rows, manifest = load_snapshot(dashboard_root / asof)
        snapshots[asof] = rows
        snapshot_manifests[asof] = manifest
        all_contract_tickers.update(
            str(row["ticker"]).upper() for row in rows
        )
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(config["paths"]["database_path"], base_dir=base_dir)
    )
    connection = connect(
        db_path,
        timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 120)),
    )
    try:
        aliases = load_aliases(
            connection,
            source_id=str(universe["ticker_aliases_source_id"]),
        )
        memberships = load_memberships(connection)
        continuity = load_continuity(connection)
        physical_tickers = set(all_contract_tickers)
        for ticker in all_contract_tickers:
            for policy in aliases.get(ticker, []):
                physical_tickers.add(str(policy["active_ticker"]))
                physical_tickers.add(str(policy["predecessor_ticker"]))
        benchmark = str(standards["primary_benchmark_ticker"]).upper()
        physical_tickers.add(benchmark)
        active_source = str(
            family["historical_load"]["active_price_source_id"]
        )
        delisted_source = str(
            family["historical_load"]["delisted_price_source_id"]
        )
        prices = load_adjusted_open_prices(
            connection,
            tickers=sorted(physical_tickers),
            sources=[active_source, delisted_source],
        )
    finally:
        connection.close()
    benchmark_windows: dict[tuple[str, int], Any] = {}
    usable_weekly_dates: list[str] = []
    for asof in selected:
        all_available = True
        for horizon in HORIZONS:
            window = execution_window(
                prices.get(benchmark, {}),
                asof=asof,
                horizon_sessions=horizon,
                source_order=[active_source, delisted_source],
            )
            benchmark_windows[(asof, horizon)] = window
            if window.return_value is None:
                all_available = False
        if all_available:
            usable_weekly_dates.append(asof)
    if len(usable_weekly_dates) < 60:
        raise ValueError(
            "Fewer than 60 weekly snapshots have complete 21/63-session "
            f"benchmark outcomes: {len(usable_weekly_dates)}"
        )
    split_map = purged_split_map(
        usable_weekly_dates,
        train_fraction=float(standards["train_fraction"]),
        validation_fraction=float(standards["validation_fraction"]),
        purge_calendar_days=int(standards["purge_calendar_days"]),
    )
    panel_rows: list[dict[str, str]] = []
    source_index: list[dict[str, object]] = []
    for asof in usable_weekly_dates:
        sidecar_path = dashboard_root / asof / SIDECAR
        rank_manifest_path = dashboard_root / asof / RANK_MANIFEST
        sidecar_sha = artifact_sha256(sidecar_path)
        manifest_sha = artifact_sha256(rank_manifest_path)
        source_index.append(
            {
                "asof_date": asof,
                "sidecar_path": str(sidecar_path),
                "sidecar_sha256": sidecar_sha,
                "rank_manifest_path": str(rank_manifest_path),
                "rank_manifest_sha256": manifest_sha,
                "row_count": len(snapshots[asof]),
            }
        )
        for source_row in snapshots[asof]:
            ticker = str(source_row["ticker"]).upper()
            membership_source = str(source_row["membership_source_id"])
            membership = memberships.get(
                (ticker, membership_source),
                {},
            )
            asof_date = parse_date(asof)
            price_ticker, alias_resolution = resolve_price_ticker(
                ticker,
                asof=asof_date,
                aliases=aliases,
            )
            continuity_policy = continuity.get(ticker, {})
            core_eligible = (
                source_row.get("stage11_calibration_input_eligible_flag")
                == "1"
                and source_row.get("calibration_use") == "core"
                and source_row.get("development_stage") == "operating"
            )
            eligibility_reason = (
                "ok"
                if core_eligible
                else "not_rank_ready"
                if source_row.get("rank_ready_flag") != "1"
                else "production_universe_operating_core_only"
            )
            preferred_sources = (
                [delisted_source, active_source]
                if membership.get("end_date")
                or source_row.get("membership_status") == "delisted"
                else [active_source, delisted_source]
            )
            for horizon in HORIZONS:
                benchmark_window = benchmark_windows[(asof, horizon)]
                horizon_end = (
                    benchmark_window.exit.bar_date
                    if benchmark_window.exit
                    else None
                )
                terminal_date_value = membership.get("end_date")
                terminal_date = (
                    terminal_date_value
                    if isinstance(terminal_date_value, date)
                    else None
                )
                security_start_value = continuity_policy.get(
                    "current_security_start_date"
                )
                security_start = (
                    security_start_value
                    if isinstance(security_start_value, date)
                    else None
                )
                structural_break_value = continuity_policy.get(
                    "structural_break_date"
                )
                structural_break = (
                    structural_break_value
                    if isinstance(structural_break_value, date)
                    else None
                )
                window = execution_window(
                    prices.get(price_ticker, {}),
                    asof=asof,
                    horizon_sessions=horizon,
                    source_order=preferred_sources,
                    terminal_date=terminal_date,
                    terminal_type=str(
                        membership.get("terminal_type") or ""
                    ),
                    horizon_end=horizon_end,
                    current_security_start_date=security_start,
                    structural_break_date=structural_break,
                )
                security_return = window.return_value
                benchmark_return = benchmark_window.return_value
                excess = (
                    security_return - benchmark_return
                    if security_return is not None
                    and benchmark_return is not None
                    else None
                )
                exit_value = (
                    window.exit.adjusted_close
                    if window.exit and window.terminal_exit
                    else window.exit.adjusted_open
                    if window.exit
                    else None
                )
                row = {
                    field: str(source_row.get(field) or "")
                    for field in [
                        "asof_date",
                        "ticker",
                        "company_name",
                        "calibration_cohort",
                        "calibration_use",
                        "development_stage",
                        "membership_source_id",
                        "membership_start_date",
                        "membership_end_date",
                        "membership_status",
                        *COMPONENT_FIELDS,
                    ]
                }
                row.update(
                    {
                        "physical_price_ticker": price_ticker,
                        "alias_resolution": alias_resolution,
                        "baseline_final_score": source_row.get(
                            "final_score", ""
                        ),
                        "rank_ready_flag": source_row.get(
                            "rank_ready_flag", ""
                        ),
                        "stage11_calibration_input_eligible_flag": (
                            source_row.get(
                                "stage11_calibration_input_eligible_flag",
                                "",
                            )
                        ),
                        "calibration_eligible_flag": (
                            "1" if core_eligible else "0"
                        ),
                        "calibration_eligible_reason": eligibility_reason,
                        "horizon_sessions": str(horizon),
                        "security_price_source_id": (
                            window.entry.source_id
                            if window.entry
                            else ""
                        ),
                        "entry_date": (
                            window.entry.bar_date.isoformat()
                            if window.entry
                            else ""
                        ),
                        "entry_adjusted_open": fmt(
                            window.entry.adjusted_open
                            if window.entry
                            else None
                        ),
                        "exit_date": (
                            window.exit.bar_date.isoformat()
                            if window.exit
                            else ""
                        ),
                        "exit_execution_value": fmt(exit_value),
                        "outcome_method": window.method,
                        "security_forward_return": fmt(
                            security_return
                        ),
                        "benchmark_ticker": benchmark,
                        "benchmark_price_source_id": (
                            benchmark_window.entry.source_id
                            if benchmark_window.entry
                            else ""
                        ),
                        "benchmark_entry_date": (
                            benchmark_window.entry.bar_date.isoformat()
                            if benchmark_window.entry
                            else ""
                        ),
                        "benchmark_exit_date": (
                            benchmark_window.exit.bar_date.isoformat()
                            if benchmark_window.exit
                            else ""
                        ),
                        "benchmark_forward_return": fmt(
                            benchmark_return
                        ),
                        "forward_excess_return": fmt(excess),
                        "outcome_available_flag": (
                            "1" if excess is not None else "0"
                        ),
                        "outcome_unavailable_reason": (
                            "" if excess is not None
                            else window.unavailable_reason
                            or benchmark_window.unavailable_reason
                        ),
                        "split": split_map[asof],
                        "source_sidecar_sha256": sidecar_sha,
                        "source_rank_manifest_sha256": manifest_sha,
                    }
                )
                panel_rows.append(
                    {
                        field: str(row.get(field) or "")
                        for field in PANEL_FIELDS
                    }
                )
    split_rows = [
        {
            "asof_date": asof,
            "split": split_map[asof],
        }
        for asof in usable_weekly_dates
    ]
    write_csv_atomic(panel_path, PANEL_FIELDS, panel_rows)
    write_csv_atomic(
        split_path,
        ["asof_date", "split"],
        split_rows,
    )
    source_index_path = (
        output_root / "transportation_generic_oos_source_index.csv"
    )
    write_csv_atomic(
        source_index_path,
        [
            "asof_date",
            "sidecar_path",
            "sidecar_sha256",
            "rank_manifest_path",
            "rank_manifest_sha256",
            "row_count",
        ],
        source_index,
    )
    manifest = {
        "artifact_family": "transportation_generic_oos_panel",
        "model_family": MODEL_FAMILY,
        "acceptance": "PASS",
        "return_basis": "next_session_open_execution_excess",
        "benchmark_ticker": benchmark,
        "horizons_sessions": list(HORIZONS),
        "snapshot_cadence": "weekly",
        "weekly_selection": standards.get("weekly_selection"),
        "survivorship_corrected": True,
        "production_universe_policy": "operating_core_only",
        "panel_path": str(panel_path),
        "panel_sha256": artifact_sha256(panel_path),
        "panel_row_count": len(panel_rows),
        "source_index_path": str(source_index_path),
        "source_index_sha256": artifact_sha256(source_index_path),
        "split_path": str(split_path),
        "split_sha256": artifact_sha256(split_path),
        "weekly_snapshot_count": len(usable_weekly_dates),
        "start_date": usable_weekly_dates[0],
        "end_date": usable_weekly_dates[-1],
        "split_counts": {
            split: sum(value == split for value in split_map.values())
            for split in sorted(set(split_map.values()))
        },
        "eligible_row_count": sum(
            row["calibration_eligible_flag"] == "1"
            for row in panel_rows
        ),
        "outcome_available_row_count": sum(
            row["outcome_available_flag"] == "1"
            for row in panel_rows
        ),
        "generator": str(Path(__file__).resolve()),
        "generator_sha256": artifact_sha256(
            Path(__file__).resolve()
        ),
        "shared_oos_module": str(
            PROJECT_ROOT / "industrials" / "core" / "oos_research.py"
        ),
        "shared_oos_module_sha256": artifact_sha256(
            PROJECT_ROOT / "industrials" / "core" / "oos_research.py"
        ),
    }
    write_text_atomic(
        manifest_path,
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
