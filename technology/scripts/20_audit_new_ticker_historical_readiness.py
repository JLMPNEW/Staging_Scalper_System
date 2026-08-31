from __future__ import annotations

import argparse
import csv
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path


DEFAULT_TICKERS = {
    "semiconductors": ("AIP",),
    "software_infrastructure": (
        "ALRM",
        "AMPL",
        "CRM",
        "DMRC",
        "FRSH",
        "MITK",
        "OCTV",
        "OOMA",
        "PEGA",
        "RDVT",
        "RNG",
        "SOUN",
        "ZM",
    ),
    "technology_hardware": ("AEVA",),
}


@dataclass(frozen=True)
class FamilyPaths:
    dashboard_root: Path
    rank_filename: str
    stage11_prefix: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit historical research readiness for newly added technology tickers."
    )
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2019, 1, 2))
    return parser.parse_args()


def family_paths(repo_root: Path) -> dict[str, FamilyPaths]:
    reports = repo_root / "output" / "technology_reports"
    return {
        "semiconductors": FamilyPaths(
            reports / "semi_dashboard",
            "semiconductor_final_rank_table.csv",
            "semiconductor",
        ),
        "software_infrastructure": FamilyPaths(
            reports / "software_infrastructure" / "dashboard",
            "software_infrastructure_final_rank_table.csv",
            "software_infrastructure",
        ),
        "technology_hardware": FamilyPaths(
            reports / "technology_hardware" / "dashboard",
            "technology_hardware_final_rank_table.csv",
            "technology_hardware",
        ),
    }


def scalar(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> object:
    row = connection.execute(sql, params).fetchone()
    return row[0] if row else None


def database_coverage(
    connection: sqlite3.Connection,
    family: str,
    ticker: str,
    audit_start: date,
) -> dict[str, object]:
    pit_start = scalar(
        connection,
        """
        SELECT MIN(start_date)
        FROM dim_universe_membership
        WHERE model_family = ? AND ticker = ? AND point_in_time_flag = 1
        """,
        (family, ticker),
    )
    expected_start = max(audit_start.isoformat(), str(pit_start or audit_start.isoformat()))
    fields: dict[str, object] = {
        "family": family,
        "ticker": ticker,
        "pit_start": pit_start,
        "expected_start": expected_start,
    }
    specifications = {
        "price": ("fact_price_ohlcv", "bar_date"),
        "sec": ("fact_sec_filing", "filing_date"),
        "financial": ("feature_financial_statement", "asof_date"),
        "form4": ("fact_sec_form4_transaction", "filing_date"),
        "institutional_13f": ("fact_13f_positioning", "asof_date"),
        "short_interest": ("fact_short_interest", "settlement_date"),
        "ibkr_borrow": ("fact_ibkr_borrow_snapshot", "asof_date"),
    }
    for label, (table, date_column) in specifications.items():
        minimum, maximum, count = connection.execute(
            f"""
            SELECT MIN({date_column}), MAX({date_column}), COUNT(*)
            FROM {table}
            WHERE ticker = ? AND {date_column} >= ?
            """,
            (ticker, expected_start),
        ).fetchone()
        fields[f"{label}_min"] = minimum
        fields[f"{label}_max"] = maximum
        fields[f"{label}_count"] = count
    return fields


def scan_csv_ticker_dates(files: list[Path], tickers: tuple[str, ...]) -> dict[str, list[str]]:
    results = {ticker: [] for ticker in tickers}
    wanted = set(tickers)
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            present = {
                str(row.get("ticker", "")).strip().upper()
                for row in csv.DictReader(handle)
            }
        for ticker in wanted.intersection(present):
            results[ticker].append(path.parent.name)
    return results


def scan_stage11_ticker_dates(
    dashboard_root: Path,
    prefix: str,
    tickers: tuple[str, ...],
) -> dict[str, list[str]]:
    results = {ticker: set() for ticker in tickers}
    wanted = set(tickers)
    files = sorted(
        (dashboard_root / "stage11_combined").glob(
            f"{prefix}_stage11_survivorship_calibration_panel_*.csv"
        )
    )
    root_panel = dashboard_root / f"{prefix}_stage11_survivorship_calibration_panel.csv"
    if root_panel.exists():
        files.append(root_panel)
    for path in files:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                ticker = str(row.get("ticker", "")).strip().upper()
                asof_date = str(row.get("asof_date", "")).strip()
                if ticker in wanted and asof_date:
                    results[ticker].add(asof_date)
    return {ticker: sorted(dates) for ticker, dates in results.items()}


def print_date_summary(label: str, ticker: str, dates: list[str]) -> None:
    minimum = min(dates) if dates else ""
    maximum = max(dates) if dates else ""
    print(f"{label}|{ticker}|count={len(dates)}|min={minimum}|max={maximum}")


def main() -> int:
    args = parse_args()
    paths = family_paths(args.repo_root.resolve())
    uri = f"file:{args.db.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        print("DATABASE_COVERAGE")
        for family, tickers in DEFAULT_TICKERS.items():
            for ticker in tickers:
                row = database_coverage(connection, family, ticker, args.start_date)
                print("|".join(f"{key}={value if value is not None else ''}" for key, value in row.items()))

    print("RANK_SNAPSHOT_COVERAGE")
    for family, tickers in DEFAULT_TICKERS.items():
        path_spec = paths[family]
        rank_files = sorted(
            path_spec.dashboard_root.glob(f"20??-??-??/{path_spec.rank_filename}")
        )
        coverage = scan_csv_ticker_dates(rank_files, tickers)
        print(
            f"RANK_FILES|{family}|count={len(rank_files)}|"
            f"min={rank_files[0].parent.name if rank_files else ''}|"
            f"max={rank_files[-1].parent.name if rank_files else ''}"
        )
        for ticker, dates in coverage.items():
            print_date_summary("RANK", ticker, dates)

    print("STAGE11_COVERAGE")
    for family, tickers in DEFAULT_TICKERS.items():
        path_spec = paths[family]
        coverage = scan_stage11_ticker_dates(
            path_spec.dashboard_root,
            path_spec.stage11_prefix,
            tickers,
        )
        for ticker, dates in coverage.items():
            print_date_summary("STAGE11", ticker, dates)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
