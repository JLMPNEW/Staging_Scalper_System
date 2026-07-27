from __future__ import annotations

import csv
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from industrials.core.reports import write_csv_atomic
from industrials.machinery.scoring import AVAILABILITY_STATUS_FIELDS, write_json_atomic


VALID_AVAILABILITY_STATUSES = frozenset(
    {
        "REPORTED",
        "PROXY",
        "EXEMPT",
        "NOT_APPLICABLE",
        "NOT_DISCLOSED",
        "DISCLOSED_UNPARSED",
        "PARSER_FAILURE",
    }
)
COVERED_STATUSES = frozenset({"REPORTED", "PROXY"})
EXCLUDED_STATUSES = frozenset({"EXEMPT", "NOT_APPLICABLE"})
UNIVERSE_CLASSES = ("active", "delisted", "combined")

DATE_COVERAGE_FIELDS = [
    "asof_date",
    "universe_class",
    "expected_ticker_count",
    "published_ticker_count",
    "rank_ready_count",
    "research_eligible_count",
    "market_feature_exact_count",
    "financial_feature_exact_count",
    "positioning_feature_exact_count",
    "fully_classified_ticker_count",
    "reported_metric_count",
    "proxy_metric_count",
    "unavailable_metric_count",
]
METRIC_COVERAGE_FIELDS = [
    "universe_class",
    "metric_name",
    "observation_count",
    "applicable_count",
    "covered_count",
    "coverage_fraction",
    "reported_count",
    "proxy_count",
    "exempt_count",
    "not_applicable_count",
    "not_disclosed_count",
    "disclosed_unparsed_count",
    "parser_failure_count",
    "unclassified_count",
    "distinct_ticker_count",
    "distinct_date_count",
    "first_asof_date",
    "last_asof_date",
]
TICKER_METRIC_COVERAGE_FIELDS = [
    "ticker",
    "universe_class",
    "metric_name",
    "observation_count",
    "applicable_count",
    "covered_count",
    "coverage_fraction",
    "reported_count",
    "proxy_count",
    "exempt_count",
    "not_applicable_count",
    "not_disclosed_count",
    "disclosed_unparsed_count",
    "parser_failure_count",
    "unclassified_count",
    "first_asof_date",
    "last_asof_date",
]


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def metric_name_from_status_field(field: str) -> str:
    suffix = "_availability_status"
    if not field.endswith(suffix):
        raise ValueError(f"Unexpected availability field: {field}")
    return field[: -len(suffix)]


def row_universe_class(row: dict[str, str]) -> str:
    return "delisted" if str(row.get("membership_status") or "") == "historical_delisted" else "active"


def _int_flag(row: dict[str, str], field: str) -> int:
    return 1 if str(row.get(field) or "").strip() == "1" else 0


def _coverage_fraction(covered: int, applicable: int) -> str:
    if applicable <= 0:
        return ""
    return f"{covered / applicable:.8f}"


def _membership_intervals(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT ticker, start_date, COALESCE(end_date, '') AS end_date,
               membership_status
        FROM dim_universe_membership
        WHERE model_family = 'machinery'
        ORDER BY ticker, start_date
        """
    ).fetchall()
    return [dict(row) for row in rows]


def expected_tickers_by_date(
    conn: sqlite3.Connection,
    dates: Iterable[str],
) -> dict[str, dict[str, set[str]]]:
    intervals = _membership_intervals(conn)
    output: dict[str, dict[str, set[str]]] = {}
    for asof in dates:
        active: set[str] = set()
        delisted: set[str] = set()
        for interval in intervals:
            if str(interval["start_date"]) > asof:
                continue
            end_date = str(interval["end_date"] or "")
            if end_date and end_date < asof:
                continue
            ticker = str(interval["ticker"])
            target = delisted if str(interval["membership_status"]) == "historical_delisted" else active
            target.add(ticker)
        output[asof] = {
            "active": active,
            "delisted": delisted,
            "combined": active | delisted,
        }
    return output


def load_validated_sidecar(output_dir: Path, *, asof: str) -> list[dict[str, str]]:
    sidecar = output_dir / "machinery_stage11_survivorship_calibration_panel.csv"
    if not sidecar.exists():
        raise FileNotFoundError(f"Missing machinery survivorship sidecar: {sidecar}")
    rows = read_csv_rows(sidecar)
    if not rows:
        raise ValueError(f"Empty machinery survivorship sidecar: {sidecar}")
    missing_fields = sorted(set(AVAILABILITY_STATUS_FIELDS) - set(rows[0]))
    if missing_fields:
        raise ValueError(
            f"Stale machinery survivorship sidecar missing current fields={missing_fields}: {sidecar}"
        )
    tickers = [str(row.get("ticker") or "") for row in rows]
    if any(not ticker for ticker in tickers):
        raise ValueError(f"Blank ticker in machinery survivorship sidecar: {sidecar}")
    if len(tickers) != len(set(tickers)):
        raise ValueError(f"Duplicate ticker in machinery survivorship sidecar: {sidecar}")
    if {str(row.get("asof_date") or "") for row in rows} != {asof}:
        raise ValueError(f"Sidecar as-of mismatch for {sidecar}; expected {asof}")
    if any(str(row.get("survivorship_corrected_panel_flag") or "") != "1" for row in rows):
        raise ValueError(f"Non-survivorship row in machinery historical sidecar: {sidecar}")
    return rows


def _new_metric_bucket() -> dict[str, Any]:
    return {
        "statuses": defaultdict(int),
        "tickers": set(),
        "dates": set(),
    }


def _metric_coverage_row(
    *,
    universe_class: str,
    metric_name: str,
    bucket: dict[str, Any],
) -> dict[str, object]:
    statuses: dict[str, int] = bucket["statuses"]
    observation_count = sum(statuses.values())
    covered_count = sum(statuses.get(status, 0) for status in COVERED_STATUSES)
    excluded_count = sum(statuses.get(status, 0) for status in EXCLUDED_STATUSES)
    applicable_count = observation_count - excluded_count
    dates = sorted(str(item) for item in bucket["dates"])
    return {
        "universe_class": universe_class,
        "metric_name": metric_name,
        "observation_count": observation_count,
        "applicable_count": applicable_count,
        "covered_count": covered_count,
        "coverage_fraction": _coverage_fraction(covered_count, applicable_count),
        "reported_count": statuses.get("REPORTED", 0),
        "proxy_count": statuses.get("PROXY", 0),
        "exempt_count": statuses.get("EXEMPT", 0),
        "not_applicable_count": statuses.get("NOT_APPLICABLE", 0),
        "not_disclosed_count": statuses.get("NOT_DISCLOSED", 0),
        "disclosed_unparsed_count": statuses.get("DISCLOSED_UNPARSED", 0),
        "parser_failure_count": statuses.get("PARSER_FAILURE", 0),
        "unclassified_count": statuses.get("", 0),
        "distinct_ticker_count": len(bucket["tickers"]),
        "distinct_date_count": len(dates),
        "first_asof_date": dates[0] if dates else "",
        "last_asof_date": dates[-1] if dates else "",
    }


def _delisted_scope_summary(
    conn: sqlite3.Connection,
    *,
    start_date: str,
) -> dict[str, object]:
    start_year = int(start_date[:4])
    seed_rows = conn.execute(
        """
        SELECT ticker, exit_year
        FROM dim_delisted_calibration_seed
        WHERE model_family = 'machinery'
        ORDER BY ticker
        """
    ).fetchall()
    membership = {
        str(row["ticker"])
        for row in conn.execute(
            """
            SELECT DISTINCT ticker
            FROM dim_universe_membership
            WHERE model_family = 'machinery'
              AND membership_status = 'historical_delisted'
            """
        ).fetchall()
    }
    in_scope = {
        str(row["ticker"])
        for row in seed_rows
        if row["exit_year"] is None or int(row["exit_year"]) >= start_year
    }
    out_of_scope = {str(row["ticker"]) for row in seed_rows} - in_scope
    unresolved = in_scope - membership
    return {
        "delisted_seed_count": len(seed_rows),
        "delisted_in_scope_candidate_count": len(in_scope),
        "delisted_resolved_membership_count": len(in_scope & membership),
        "delisted_unresolved_count": len(unresolved),
        "delisted_unresolved_tickers": sorted(unresolved),
        "delisted_pre_start_out_of_scope_count": len(out_of_scope),
    }


def build_combined_historical_coverage(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    dashboard_root: Path,
    report_root: Path,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    if not dates:
        raise ValueError("Combined historical coverage requires at least one date")
    expected = expected_tickers_by_date(conn, dates)
    errors: list[str] = []
    date_rows: list[dict[str, object]] = []
    metric_buckets: dict[tuple[str, str], dict[str, Any]] = defaultdict(_new_metric_bucket)
    ticker_metric_buckets: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(_new_metric_bucket)
    total_published_rows = 0

    for asof in dates:
        try:
            rows = load_validated_sidecar(dashboard_root / asof, asof=asof)
        except (FileNotFoundError, ValueError) as exc:
            errors.append(str(exc))
            continue
        published = {str(row["ticker"]) for row in rows}
        if published != expected[asof]["combined"]:
            missing = sorted(expected[asof]["combined"] - published)
            extra = sorted(published - expected[asof]["combined"])
            errors.append(f"{asof}: membership mismatch missing={missing} extra={extra}")
        for universe_class in ("active", "delisted"):
            published_class = {
                str(row["ticker"])
                for row in rows
                if row_universe_class(row) == universe_class
            }
            if published_class != expected[asof][universe_class]:
                missing = sorted(expected[asof][universe_class] - published_class)
                extra = sorted(published_class - expected[asof][universe_class])
                errors.append(
                    f"{asof}: {universe_class} membership-class mismatch "
                    f"missing={missing} extra={extra}"
                )
        total_published_rows += len(rows)

        for universe_class in UNIVERSE_CLASSES:
            selected = rows if universe_class == "combined" else [
                row for row in rows if row_universe_class(row) == universe_class
            ]
            date_rows.append(
                {
                    "asof_date": asof,
                    "universe_class": universe_class,
                    "expected_ticker_count": len(expected[asof][universe_class]),
                    "published_ticker_count": len(selected),
                    "rank_ready_count": sum(_int_flag(row, "rank_ready_flag") for row in selected),
                    "research_eligible_count": sum(
                        _int_flag(row, "stage11_calibration_input_eligible_flag") for row in selected
                    ),
                    "market_feature_exact_count": sum(
                        str(row.get("market_feature_asof_date") or "") == asof for row in selected
                    ),
                    "financial_feature_exact_count": sum(
                        str(row.get("financial_feature_asof_date") or "") == asof for row in selected
                    ),
                    "positioning_feature_exact_count": sum(
                        str(row.get("positioning_feature_asof_date") or "") == asof for row in selected
                    ),
                    "fully_classified_ticker_count": sum(
                        math.isclose(
                            float(str(row.get("financial_metric_classified_fraction") or "nan")),
                            1.0,
                            rel_tol=0.0,
                            abs_tol=1e-9,
                        )
                        for row in selected
                    ),
                    "reported_metric_count": sum(
                        int(str(row.get("financial_metric_reported_count") or "0")) for row in selected
                    ),
                    "proxy_metric_count": sum(
                        int(str(row.get("financial_metric_proxy_count") or "0")) for row in selected
                    ),
                    "unavailable_metric_count": sum(
                        int(str(row.get("financial_metric_unavailable_count") or "0")) for row in selected
                    ),
                }
            )

        for row in rows:
            ticker = str(row["ticker"])
            row_class = row_universe_class(row)
            for field in AVAILABILITY_STATUS_FIELDS:
                metric_name = metric_name_from_status_field(field)
                status = str(row.get(field) or "")
                if status not in VALID_AVAILABILITY_STATUSES:
                    errors.append(f"{asof}:{ticker}:{metric_name}: invalid status={status!r}")
                for universe_class in (row_class, "combined"):
                    bucket = metric_buckets[(universe_class, metric_name)]
                    bucket["statuses"][status] += 1
                    bucket["tickers"].add(ticker)
                    bucket["dates"].add(asof)
                ticker_bucket = ticker_metric_buckets[(ticker, row_class, metric_name)]
                ticker_bucket["statuses"][status] += 1
                ticker_bucket["tickers"].add(ticker)
                ticker_bucket["dates"].add(asof)

    metric_rows = [
        _metric_coverage_row(
            universe_class=universe_class,
            metric_name=metric_name,
            bucket=bucket,
        )
        for (universe_class, metric_name), bucket in sorted(metric_buckets.items())
    ]
    ticker_metric_rows: list[dict[str, object]] = []
    for (ticker, universe_class, metric_name), bucket in sorted(ticker_metric_buckets.items()):
        row = _metric_coverage_row(
            universe_class=universe_class,
            metric_name=metric_name,
            bucket=bucket,
        )
        ticker_metric_rows.append(
            {
                "ticker": ticker,
                **{field: row[field] for field in TICKER_METRIC_COVERAGE_FIELDS if field != "ticker"},
            }
        )

    report_root.mkdir(parents=True, exist_ok=True)
    date_csv = report_root / "machinery_combined_historical_coverage_by_date.csv"
    metric_csv = report_root / "machinery_combined_historical_metric_coverage.csv"
    ticker_metric_csv = report_root / "machinery_combined_historical_ticker_metric_coverage.csv"
    write_csv_atomic(date_csv, DATE_COVERAGE_FIELDS, date_rows)
    write_csv_atomic(metric_csv, METRIC_COVERAGE_FIELDS, metric_rows)
    write_csv_atomic(ticker_metric_csv, TICKER_METRIC_COVERAGE_FIELDS, ticker_metric_rows)

    scope = _delisted_scope_summary(conn, start_date=start_date)
    summary: dict[str, object] = {
        "acceptance": "PASS" if not errors and len(date_rows) == len(dates) * len(UNIVERSE_CLASSES) else "FAIL",
        "start_date": start_date,
        "end_date": end_date,
        "scheduled_date_count": len(dates),
        "validated_date_count": len(date_rows) // len(UNIVERSE_CLASSES),
        "published_observation_count": total_published_rows,
        "metric_count": len(AVAILABILITY_STATUS_FIELDS),
        "errors": errors,
        "coverage_by_date_csv": str(date_csv),
        "metric_coverage_csv": str(metric_csv),
        "ticker_metric_coverage_csv": str(ticker_metric_csv),
        **scope,
    }
    write_json_atomic(report_root / "machinery_combined_historical_coverage.json", summary)
    return summary
