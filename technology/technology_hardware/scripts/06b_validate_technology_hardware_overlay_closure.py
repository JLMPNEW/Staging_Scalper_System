#!/usr/bin/env python3
"""Close technology hardware Stage 6B as a deliberate neutral/no-overlay stage."""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402


LOGGER = logging.getLogger("technology_hardware_stage6b")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate technology hardware Stage 6B neutral overlay closure.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--asof", default="", help="Stage 6A as-of date. Defaults to latest available hardware scoring date.")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def ro_connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["check_name", "status", "detail", "checked_at"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = args.output_csv.expanduser().resolve() if args.output_csv else resolve_path(
        cfg_get(
            config,
            "technology_hardware_stage6b_overlay_closure.output_csv",
            "../output/technology_reports/technology_hardware/scoring/technology_hardware_stage6b_overlay_closure.csv",
        ),
        base_dir=base_dir,
    )
    model_family = str(cfg_get(config, "technology_hardware_scoring_features.model_family", "technology_hardware"))
    source_id = str(cfg_get(config, "technology_hardware_scoring_features.source_id", "technology_hardware_scoring_contract"))
    neutral_score = float(cfg_get(config, "technology_hardware_scoring_features.sector_overlay_default_score", 50.0))
    checked_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    stage6_overlays = cfg_get(config, "technology_hardware_scoring_features.sector_overlay_components", [])
    stage7_overlays = cfg_get(config, "technology_hardware_calibrated_scoring.sector_overlay_components", [])
    overlay_weight = float(cfg_get(config, "technology_hardware_calibrated_scoring.overlay_weight", 0.0))
    if stage6_overlays:
        errors.append(f"Stage 6A overlay components are configured: {stage6_overlays}")
    if stage7_overlays:
        errors.append(f"Stage 7 overlay components are configured: {stage7_overlays}")
    if abs(overlay_weight) > 1e-12:
        errors.append(f"Stage 7 overlay_weight is nonzero: {overlay_weight}")

    rows.append(
        {
            "check_name": "hardware_overlay_config",
            "status": "pass" if not stage6_overlays and not stage7_overlays and abs(overlay_weight) <= 1e-12 else "fail",
            "detail": f"stage6_components={stage6_overlays}; stage7_components={stage7_overlays}; overlay_weight={overlay_weight}",
            "checked_at": checked_at,
        }
    )

    with ro_connect(db_path) as conn:
        asof = args.asof.strip()
        if not asof:
            row = conn.execute(
                """
                SELECT MAX(asof_date)
                FROM feature_scoring_input
                WHERE source_id = ? AND model_family = ?
                """,
                (source_id, model_family),
            ).fetchone()
            asof = str(row[0] or "")
        if not asof:
            errors.append("No Stage 6A technology hardware scoring rows found.")
            total_rows = 0
            bad_rows = 0
        else:
            total_rows = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM feature_scoring_input
                    WHERE source_id = ? AND model_family = ? AND asof_date = ?
                    """,
                    (source_id, model_family, asof),
                ).fetchone()[0]
                or 0
            )
            bad_rows = int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM feature_scoring_input
                    WHERE source_id = ? AND model_family = ? AND asof_date = ?
                      AND (
                        ABS(COALESCE(sector_overlay_score, 50.0) - ?) > 0.000001
                        OR COALESCE(sector_overlay_quality, 0.0) <> 0.0
                        OR COALESCE(sector_overlay_status, '') <> 'not_loaded'
                      )
                    """,
                    (source_id, model_family, asof, neutral_score),
                ).fetchone()[0]
                or 0
            )
            if total_rows <= 0:
                errors.append(f"No Stage 6A technology hardware scoring rows found for asof={asof}.")
            if bad_rows:
                errors.append(f"{bad_rows} Stage 6A rows have non-neutral overlay state for asof={asof}.")

    rows.append(
        {
            "check_name": "stage6a_neutral_overlay_state",
            "status": "pass" if total_rows > 0 and bad_rows == 0 else "fail",
            "detail": f"asof={asof}; rows={total_rows}; non_neutral_overlay_rows={bad_rows}; expected_score={neutral_score}; expected_quality=0; expected_status=not_loaded",
            "checked_at": checked_at,
        }
    )
    rows.append(
        {
            "check_name": "stage6b_status",
            "status": "pass" if not errors else "fail",
            "detail": "closed_as_neutral_no_overlay_configured" if not errors else " | ".join(errors),
            "checked_at": checked_at,
        }
    )
    write_csv(output_csv, rows)
    if errors:
        for error in errors:
            LOGGER.error(error)
        LOGGER.error("Technology hardware Stage 6B closure validation failed; report=%s", output_csv)
        return 1
    LOGGER.info("Technology hardware Stage 6B is closed as neutral/no-overlay; report=%s", output_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
