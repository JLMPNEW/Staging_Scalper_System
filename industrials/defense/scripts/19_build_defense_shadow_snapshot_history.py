#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from industrials.core.logging_utils import configure_utc_logging  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
FIELDNAMES = [
    "asof_date",
    "active_tickers",
    "market_covered",
    "financial_covered",
    "positioning_covered",
    "coverage_threshold",
    "snapshot_status",
    "message",
]


@dataclass(frozen=True)
class SnapshotCandidate:
    asof_date: str
    active_tickers: int
    market_covered: int
    financial_covered: int
    positioning_covered: int

    def is_publishable(self, threshold: float) -> bool:
        required = self.active_tickers * threshold
        return (
            self.active_tickers > 0
            and self.market_covered >= required
            and self.financial_covered >= required
            and self.positioning_covered >= required
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build immutable defense shadow rank-table snapshots from loaded PIT features.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--coverage-threshold", type=float, default=1.0)
    parser.add_argument("--max-dates", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-csv", type=Path, default=None)
    return parser.parse_args()


def parse_date(raw: object) -> date | None:
    text = str(raw or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"Invalid date: {raw}") from exc


def active_sql() -> str:
    return """
        SELECT DISTINCT c.ticker
        FROM dim_company c
        JOIN dim_industrials_taxonomy t ON t.company_id = c.company_id
        WHERE c.is_active = 1 AND t.model_family = 'defense'
    """


def coverage_by_date(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_id: str,
    start_date: date | None,
    end_date: date | None,
) -> dict[str, int]:
    filters = ["f.model_family = ?", "f.source_id = ?"]
    params: list[Any] = [MODEL_FAMILY, source_id]
    if start_date is not None:
        filters.append("f.asof_date >= ?")
        params.append(start_date.isoformat())
    if end_date is not None:
        filters.append("f.asof_date <= ?")
        params.append(end_date.isoformat())
    where_clause = " AND ".join(filters)
    rows = conn.execute(
        f"""
        SELECT f.asof_date, COUNT(DISTINCT f.ticker) AS covered
        FROM {table} f
        JOIN ({active_sql()}) a ON a.ticker = f.ticker
        WHERE {where_clause}
        GROUP BY f.asof_date
        ORDER BY f.asof_date
        """,
        params,
    ).fetchall()
    return {str(row["asof_date"]): int(row["covered"] or 0) for row in rows}


def load_candidates(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    start_date: date | None,
    end_date: date | None,
) -> list[SnapshotCandidate]:
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    active_count = int(conn.execute(f"SELECT COUNT(*) FROM ({active_sql()})").fetchone()[0] or 0)
    market = coverage_by_date(
        conn,
        table="feature_market_technical",
        source_id=market_source,
        start_date=start_date,
        end_date=end_date,
    )
    financial = coverage_by_date(
        conn,
        table="feature_financial_statement",
        source_id=financial_source,
        start_date=start_date,
        end_date=end_date,
    )
    positioning = coverage_by_date(
        conn,
        table="feature_positioning",
        source_id=positioning_source,
        start_date=start_date,
        end_date=end_date,
    )
    dates = sorted(set(market) | set(financial) | set(positioning))
    return [
        SnapshotCandidate(
            asof_date=asof,
            active_tickers=active_count,
            market_covered=market.get(asof, 0),
            financial_covered=financial.get(asof, 0),
            positioning_covered=positioning.get(asof, 0),
        )
        for asof in dates
    ]


def manifest_valid(snapshot_dir: Path, asof: str) -> bool:
    csv_path = snapshot_dir / "defense_final_rank_table.csv"
    manifest_path = snapshot_dir / "defense_final_rank_table_manifest.json"
    if not csv_path.exists() or not manifest_path.exists():
        return False
    validator = PROJECT_ROOT / "industrials" / "defense" / "scripts" / "18_validate_defense_shadow_rank_table.py"
    completed = subprocess.run(
        [sys.executable, str(validator), "--asof", asof],
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def run_step(script: str, asof: str) -> None:
    subprocess.run(
        [sys.executable, script, "--asof", asof],
        cwd=str(PROJECT_ROOT),
        check=True,
    )


def write_report(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if not 0.0 < args.coverage_threshold <= 1.0:
        raise ValueError("--coverage-threshold must be > 0 and <= 1")
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "stage6" / "shadow_snapshot_history_build_report.csv"
    )
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if start_date and end_date and start_date > end_date:
        raise ValueError("--start-date cannot be after --end-date")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        candidates = load_candidates(conn, config=config, start_date=start_date, end_date=end_date)

    publishable = [candidate for candidate in candidates if candidate.is_publishable(args.coverage_threshold)]
    if args.max_dates > 0:
        publishable = publishable[-args.max_dates :]
    if not publishable:
        write_report(
            output_csv,
            [
                {
                    "asof_date": "",
                    "active_tickers": 0,
                    "market_covered": 0,
                    "financial_covered": 0,
                    "positioning_covered": 0,
                    "coverage_threshold": args.coverage_threshold,
                    "snapshot_status": "no_publishable_dates",
                    "message": "No dates have enough loaded Stage 3/4/5 feature coverage.",
                }
            ],
        )
        raise ValueError("No publishable defense shadow snapshot dates found")

    publisher = str(PROJECT_ROOT / "industrials" / "defense" / "scripts" / "17_publish_defense_shadow_rank_table.py")
    validator = str(PROJECT_ROOT / "industrials" / "defense" / "scripts" / "18_validate_defense_shadow_rank_table.py")
    report_rows: list[dict[str, object]] = []
    for candidate in publishable:
        snapshot_dir = PROJECT_ROOT / "output" / "industrials" / "defense" / "dashboard" / candidate.asof_date
        if manifest_valid(snapshot_dir, candidate.asof_date):
            status = "valid_existing"
            message = "Existing immutable snapshot passed validation."
        elif args.dry_run:
            status = "would_publish"
            message = "Publishable date found; dry-run did not write output."
        else:
            run_step(publisher, candidate.asof_date)
            run_step(validator, candidate.asof_date)
            status = "published"
            message = "Snapshot published and validated."
        report_rows.append(
            {
                "asof_date": candidate.asof_date,
                "active_tickers": candidate.active_tickers,
                "market_covered": candidate.market_covered,
                "financial_covered": candidate.financial_covered,
                "positioning_covered": candidate.positioning_covered,
                "coverage_threshold": args.coverage_threshold,
                "snapshot_status": status,
                "message": message,
            }
        )
    write_report(output_csv, report_rows)
    for row in report_rows:
        print(
            f"{row['asof_date']}: {row['snapshot_status']} "
            f"market={row['market_covered']}/{row['active_tickers']} "
            f"financial={row['financial_covered']}/{row['active_tickers']} "
            f"positioning={row['positioning_covered']}/{row['active_tickers']}"
        )
    print(f"Wrote {output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
