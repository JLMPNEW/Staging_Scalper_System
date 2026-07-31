#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
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
from industrials.core.reports import write_csv_atomic  # noqa: E402
from industrials.defense.research_artifacts import select_weekly_snapshot_dates  # noqa: E402


DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
MODEL_FAMILY = "defense"
DEFAULT_SCORE_MODEL_VERSION = "defense_shadow_v0.1.0"
FIELDNAMES = [
    "asof_date",
    "active_tickers",
    "market_covered",
    "financial_covered",
    "positioning_covered",
    "coverage_threshold",
    "cadence",
    "weekly_start_date",
    "weekly_selection",
    "policy_asof_date",
    "membership_mode",
    "scoring_mode",
    "score_model_version",
    "research_candidate",
    "evaluation_calendar",
    "snapshot_root",
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
    parser.add_argument("--cadence", choices=["available", "daily", "weekly"], default="available")
    parser.add_argument("--weekly-start-date", default="", help="Weekly bucket anchor date when --cadence weekly.")
    parser.add_argument("--weekly-selection", choices=["first", "last"], default="last")
    parser.add_argument(
        "--date-order",
        choices=["oldest", "newest"],
        default="newest",
        help="When --max-dates is set, choose the oldest or newest publishable dates. Defaults to newest for compatibility.",
    )
    parser.add_argument(
        "--policy-asof",
        default="",
        help="Eligibility-policy lock date passed to the rank publisher for historical research replays.",
    )
    parser.add_argument(
        "--membership-mode",
        choices=["current", "pit"],
        default="current",
        help="current uses today's active universe; pit uses membership effective at each candidate asof.",
    )
    parser.add_argument(
        "--snapshot-root",
        type=Path,
        default=None,
        help="Root directory for dated rank snapshots. Defaults to configured dashboard root.",
    )
    parser.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="Rebuild existing dated snapshots. Intended for research roots after upstream data fixes.",
    )
    parser.add_argument("--scoring-mode", choices=["baseline", "specialized_v1"], default="baseline")
    parser.add_argument("--score-model-version", default="")
    parser.add_argument("--research-candidate", action="store_true")
    parser.add_argument(
        "--evaluation-calendar",
        type=Path,
        default=None,
        help="Frozen one-column asof_date CSV. Every listed date must be publishable and is used exactly once.",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Before applying --max-dates, ignore already sealed and valid snapshots.",
    )
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


def parse_source_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        values = [part.strip() for part in raw.split(",")]
    else:
        values = [str(part).strip() for part in (raw or [])]
    return [value for value in values if value]


def source_priority_list(primary_source: str, fallback_sources: list[str]) -> list[str]:
    out: list[str] = []
    for source_id in [primary_source, *fallback_sources]:
        if source_id and source_id not in out:
            out.append(source_id)
    if not out:
        raise ValueError("At least one source_id is required")
    return out


def placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def coverage_by_date(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_ids: list[str],
    start_date: date | None,
    end_date: date | None,
) -> dict[str, int]:
    filters = ["f.model_family = ?", f"f.source_id IN ({placeholders(source_ids)})"]
    params: list[Any] = [MODEL_FAMILY, *source_ids]
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


def feature_dates(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_ids: list[str],
    start_date: date | None,
    end_date: date | None,
) -> set[str]:
    filters = ["model_family = ?", f"source_id IN ({placeholders(source_ids)})"]
    params: list[Any] = [MODEL_FAMILY, *source_ids]
    if start_date is not None:
        filters.append("asof_date >= ?")
        params.append(start_date.isoformat())
    if end_date is not None:
        filters.append("asof_date <= ?")
        params.append(end_date.isoformat())
    rows = conn.execute(
        f"""
        SELECT DISTINCT asof_date
        FROM {table}
        WHERE {" AND ".join(filters)}
        """,
        params,
    ).fetchall()
    return {str(row["asof_date"]) for row in rows}


def pit_member_count(conn: sqlite3.Connection, *, asof: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT m.ticker)
            FROM dim_universe_membership m
            JOIN dim_industrials_taxonomy t ON t.company_id = m.company_id AND t.model_family = m.model_family
            WHERE m.model_family = ?
              AND m.point_in_time_flag = 1
              AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            """,
            (MODEL_FAMILY, asof, asof),
        ).fetchone()[0]
        or 0
    )


def pit_coverage_on_date(
    conn: sqlite3.Connection,
    *,
    table: str,
    source_ids: list[str],
    asof: str,
) -> int:
    return int(
        conn.execute(
            f"""
            SELECT COUNT(DISTINCT f.ticker)
            FROM {table} f
            JOIN dim_universe_membership m
              ON m.ticker = f.ticker AND m.model_family = f.model_family
            JOIN dim_industrials_taxonomy t
              ON t.company_id = m.company_id AND t.model_family = m.model_family
            WHERE f.model_family = ?
              AND f.source_id IN ({placeholders(source_ids)})
              AND f.asof_date = ?
              AND m.point_in_time_flag = 1
              AND m.start_date <= ?
              AND COALESCE(m.end_date, '9999-12-31') >= ?
            """,
            (MODEL_FAMILY, *source_ids, asof, asof, asof),
        ).fetchone()[0]
        or 0
    )


def load_pit_candidates(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    start_date: date | None,
    end_date: date | None,
    requested_dates: list[str] | None = None,
) -> list[SnapshotCandidate]:
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    market_sources = source_priority_list(
        market_source,
        parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", [])),
    )
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    dates = (
        list(requested_dates)
        if requested_dates is not None
        else sorted(
            feature_dates(
                conn,
                table="feature_market_technical",
                source_ids=market_sources,
                start_date=start_date,
                end_date=end_date,
            )
            | feature_dates(
                conn,
                table="feature_financial_statement",
                source_ids=[financial_source],
                start_date=start_date,
                end_date=end_date,
            )
            | feature_dates(
                conn,
                table="feature_positioning",
                source_ids=[positioning_source],
                start_date=start_date,
                end_date=end_date,
            )
        )
    )
    return [
        SnapshotCandidate(
            asof_date=asof,
            active_tickers=pit_member_count(conn, asof=asof),
            market_covered=pit_coverage_on_date(
                conn,
                table="feature_market_technical",
                source_ids=market_sources,
                asof=asof,
            ),
            financial_covered=pit_coverage_on_date(
                conn,
                table="feature_financial_statement",
                source_ids=[financial_source],
                asof=asof,
            ),
            positioning_covered=pit_coverage_on_date(
                conn,
                table="feature_positioning",
                source_ids=[positioning_source],
                asof=asof,
            ),
        )
        for asof in dates
    ]


def load_candidates(
    conn: sqlite3.Connection,
    *,
    config: dict[str, Any],
    start_date: date | None,
    end_date: date | None,
    membership_mode: str,
    requested_dates: list[str] | None = None,
) -> list[SnapshotCandidate]:
    if membership_mode == "pit":
        return load_pit_candidates(
            conn,
            config=config,
            start_date=start_date,
            end_date=end_date,
            requested_dates=requested_dates,
        )
    market_source = str(cfg_get(config, "market_feature_build.source_id", "yahoo_finance_adjusted"))
    market_sources = source_priority_list(
        market_source,
        parse_source_list(cfg_get(config, "market_data_policy.scoring_fallback_sources", [])),
    )
    financial_source = str(cfg_get(config, "sec_fundamentals.companyfacts_source_id", "sec_companyfacts"))
    positioning_source = str(cfg_get(config, "positioning_import.source_id", "industrials_positioning_composite"))
    active_count = int(conn.execute(f"SELECT COUNT(*) FROM ({active_sql()})").fetchone()[0] or 0)
    market = coverage_by_date(
        conn,
        table="feature_market_technical",
        source_ids=market_sources,
        start_date=start_date,
        end_date=end_date,
    )
    financial = coverage_by_date(
        conn,
        table="feature_financial_statement",
        source_ids=[financial_source],
        start_date=start_date,
        end_date=end_date,
    )
    positioning = coverage_by_date(
        conn,
        table="feature_positioning",
        source_ids=[positioning_source],
        start_date=start_date,
        end_date=end_date,
    )
    dates = list(requested_dates) if requested_dates is not None else sorted(set(market) | set(financial) | set(positioning))
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


def manifest_valid(
    snapshot_dir: Path,
    asof: str,
    *,
    membership_mode: str,
    scoring_mode: str,
    score_model_version: str,
    research_candidate: bool,
) -> bool:
    csv_path = snapshot_dir / "defense_final_rank_table.csv"
    manifest_path = snapshot_dir / "defense_final_rank_table_manifest.json"
    if not csv_path.exists() or not manifest_path.exists():
        return False
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError:
        return False
    if str(manifest.get("membership_mode") or "current") != membership_mode:
        return False
    if str(manifest.get("scoring_mode") or "baseline") != scoring_mode:
        return False
    if str(manifest.get("score_model_version") or "") != score_model_version:
        return False
    if bool(manifest.get("research_candidate", False)) is not research_candidate:
        return False
    validator = PROJECT_ROOT / "industrials" / "defense" / "scripts" / "18_validate_defense_shadow_rank_table.py"
    completed = subprocess.run(
        [sys.executable, str(validator), "--asof", asof, "--rank-table", str(csv_path)],
        cwd=str(PROJECT_ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def run_step(
    script: str,
    asof: str,
    *,
    policy_asof: str = "",
    output_dir: Path | None = None,
    membership_mode: str = "",
    rank_table: Path | None = None,
    allow_overwrite: bool = False,
    scoring_mode: str = "",
    score_model_version: str = "",
    research_candidate: bool = False,
) -> None:
    command = [sys.executable, script, "--asof", asof]
    if policy_asof:
        command.extend(["--policy-asof", policy_asof])
    if output_dir is not None:
        command.extend(["--output-dir", str(output_dir)])
    if membership_mode:
        command.extend(["--membership-mode", membership_mode])
    if rank_table is not None:
        command.extend(["--rank-table", str(rank_table)])
    if allow_overwrite:
        command.append("--allow-overwrite")
    if scoring_mode:
        command.extend(["--scoring-mode", scoring_mode])
    if score_model_version:
        command.extend(["--score-model-version", score_model_version])
    if research_candidate:
        command.append("--research-candidate")
    subprocess.run(
        command,
        cwd=str(PROJECT_ROOT),
        check=True,
    )


def filter_weekly_candidates(
    candidates: list[SnapshotCandidate],
    *,
    weekly_start_date: str,
    weekly_selection: str,
) -> list[SnapshotCandidate]:
    if not weekly_start_date:
        raise ValueError("--weekly-start-date is required when --cadence weekly")
    selected = set(
        select_weekly_snapshot_dates(
            [candidate.asof_date for candidate in candidates],
            weekly_start_date=weekly_start_date,
            selection=weekly_selection,
        )
    )
    return [candidate for candidate in candidates if candidate.asof_date in selected]


def read_evaluation_calendar(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Evaluation calendar does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    dates = [str(row.get("asof_date") or "").strip() for row in rows]
    if not dates or any(not value for value in dates):
        raise ValueError(f"Evaluation calendar must contain nonblank asof_date rows: {path}")
    parsed = [parse_date(value) for value in dates]
    normalized = [value.isoformat() for value in parsed if value is not None]
    if len(normalized) != len(dates) or normalized != sorted(set(normalized)):
        raise ValueError(f"Evaluation calendar dates must be valid, unique, and ascending: {path}")
    return normalized


def main() -> int:
    configure_utc_logging()
    args = parse_args()
    if not 0.0 < args.coverage_threshold <= 1.0:
        raise ValueError("--coverage-threshold must be > 0 and <= 1")
    if args.research_candidate and not args.score_model_version.strip():
        raise ValueError("--research-candidate requires --score-model-version")
    if args.scoring_mode != "baseline" and not args.research_candidate:
        raise ValueError("Non-baseline scoring modes require --research-candidate")
    score_model_version = str(args.score_model_version or DEFAULT_SCORE_MODEL_VERSION).strip()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    snapshot_root = (
        args.snapshot_root.expanduser().resolve()
        if args.snapshot_root
        else resolve_path(
            str(
                cfg_get(
                    config,
                    "oos_calibration_standards.families.defense.snapshot_history_root",
                    "../output/industrials/defense/dashboard",
                )
            ),
            base_dir=base_dir,
        )
    )
    output_csv = (
        args.output_csv.expanduser().resolve()
        if args.output_csv
        else PROJECT_ROOT / "output" / "industrials" / "defense" / "stage6" / "shadow_snapshot_history_build_report.csv"
    )
    start_date = parse_date(args.start_date)
    end_date = parse_date(args.end_date)
    if start_date and end_date and start_date > end_date:
        raise ValueError("--start-date cannot be after --end-date")
    evaluation_calendar = (
        args.evaluation_calendar.expanduser().resolve()
        if args.evaluation_calendar
        else None
    )
    requested_dates = read_evaluation_calendar(evaluation_calendar) if evaluation_calendar else None

    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        candidates = load_candidates(
            conn,
            config=config,
            start_date=start_date,
            end_date=end_date,
            membership_mode=args.membership_mode,
            requested_dates=requested_dates,
        )

    publishable = [candidate for candidate in candidates if candidate.is_publishable(args.coverage_threshold)]
    if evaluation_calendar is not None:
        assert requested_dates is not None
        by_date = {candidate.asof_date: candidate for candidate in publishable}
        missing_dates = [asof for asof in requested_dates if asof not in by_date]
        if missing_dates:
            raise ValueError(
                "Frozen evaluation calendar contains dates without complete PIT feature coverage: "
                f"{missing_dates[:20]}"
            )
        publishable = [by_date[asof] for asof in requested_dates]
    elif args.cadence == "weekly":
        publishable = filter_weekly_candidates(
            publishable,
            weekly_start_date=args.weekly_start_date,
            weekly_selection=args.weekly_selection,
        )
    if args.skip_existing and not args.allow_overwrite:
        publishable = [
            candidate
            for candidate in publishable
            if not manifest_valid(
                snapshot_root / candidate.asof_date,
                candidate.asof_date,
                membership_mode=args.membership_mode,
                scoring_mode=args.scoring_mode,
                score_model_version=score_model_version,
                research_candidate=bool(args.research_candidate),
            )
        ]
    if args.max_dates > 0:
        if args.date_order == "oldest":
            publishable = publishable[: args.max_dates]
        else:
            publishable = publishable[-args.max_dates :]
    if not publishable:
        write_csv_atomic(
            output_csv,
            FIELDNAMES,
            [
                {
                    "asof_date": "",
                    "active_tickers": 0,
                    "market_covered": 0,
                    "financial_covered": 0,
                    "positioning_covered": 0,
                    "coverage_threshold": args.coverage_threshold,
                    "cadence": args.cadence,
                    "weekly_start_date": args.weekly_start_date,
                    "weekly_selection": args.weekly_selection,
                    "policy_asof_date": args.policy_asof,
                    "membership_mode": args.membership_mode,
                    "scoring_mode": args.scoring_mode,
                    "score_model_version": score_model_version,
                    "research_candidate": int(bool(args.research_candidate)),
                    "evaluation_calendar": str(evaluation_calendar or ""),
                    "snapshot_root": str(snapshot_root),
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
        snapshot_dir = snapshot_root / candidate.asof_date
        rank_table = snapshot_dir / "defense_final_rank_table.csv"
        if not args.allow_overwrite and manifest_valid(
            snapshot_dir,
            candidate.asof_date,
            membership_mode=args.membership_mode,
            scoring_mode=args.scoring_mode,
            score_model_version=score_model_version,
            research_candidate=bool(args.research_candidate),
        ):
            status = "valid_existing"
            message = "Existing immutable snapshot passed validation."
        elif args.dry_run:
            status = "would_publish"
            message = "Publishable date found; dry-run did not write output."
        else:
            run_step(
                publisher,
                candidate.asof_date,
                policy_asof=str(args.policy_asof or ""),
                output_dir=snapshot_dir,
                membership_mode=args.membership_mode,
                allow_overwrite=bool(args.allow_overwrite),
                scoring_mode=args.scoring_mode,
                score_model_version=score_model_version,
                research_candidate=bool(args.research_candidate),
            )
            run_step(validator, candidate.asof_date, rank_table=rank_table)
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
                "cadence": args.cadence,
                "weekly_start_date": args.weekly_start_date,
                "weekly_selection": args.weekly_selection,
                "policy_asof_date": args.policy_asof,
                "membership_mode": args.membership_mode,
                "scoring_mode": args.scoring_mode,
                "score_model_version": score_model_version,
                "research_candidate": int(bool(args.research_candidate)),
                "evaluation_calendar": str(evaluation_calendar or ""),
                "snapshot_root": str(snapshot_root),
                "snapshot_status": status,
                "message": message,
            }
        )
    write_csv_atomic(output_csv, FIELDNAMES, report_rows)
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
