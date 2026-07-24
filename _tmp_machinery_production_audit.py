from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


COVERED_STATUSES = ("REPORTED", "PROXY")
EXCLUDED_STATUSES = ("EXEMPT", "NOT_APPLICABLE")


def table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]


def defense_table_digest(
    connection: sqlite3.Connection,
    *,
    table: str,
    columns: list[str],
) -> tuple[int, str]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    query = (
        f'SELECT {quoted} FROM "{table}" '
        f"WHERE model_family = 'defense' ORDER BY {quoted}"
    )
    digest = hashlib.sha256()
    count = 0
    for row in connection.execute(query):
        digest.update(
            json.dumps(
                list(row),
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def defense_comparison(
    current: sqlite3.Connection,
    baseline_path: Path,
) -> dict[str, Any]:
    baseline = sqlite3.connect(
        f"file:{baseline_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=60,
    )
    try:
        current_tables = {
            str(row[0])
            for row in current.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        baseline_tables = {
            str(row[0])
            for row in baseline.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        results: list[dict[str, Any]] = []
        for table in sorted(current_tables & baseline_tables):
            columns = table_columns(current, table)
            if "model_family" not in columns:
                continue
            baseline_columns = table_columns(baseline, table)
            if columns != baseline_columns:
                results.append(
                    {
                        "table": table,
                        "status": "SCHEMA_MISMATCH",
                        "current_columns": columns,
                        "baseline_columns": baseline_columns,
                    }
                )
                continue
            current_count, current_digest = defense_table_digest(
                current,
                table=table,
                columns=columns,
            )
            baseline_count, baseline_digest = defense_table_digest(
                baseline,
                table=table,
                columns=columns,
            )
            results.append(
                {
                    "table": table,
                    "status": (
                        "MATCH"
                        if (current_count, current_digest)
                        == (baseline_count, baseline_digest)
                        else "DIFFERENT"
                    ),
                    "current_count": current_count,
                    "baseline_count": baseline_count,
                    "current_sha256": current_digest,
                    "baseline_sha256": baseline_digest,
                }
            )
        return {
            "acceptance": (
                "PASS"
                if results
                and all(row["status"] == "MATCH" for row in results)
                else "FAIL"
            ),
            "table_count": len(results),
            "tables": results,
        }
    finally:
        baseline.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--asof", default="2026-07-22")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--quick-check", action="store_true")
    args = parser.parse_args()

    database = args.database.expanduser().resolve()
    connection = sqlite3.connect(
        f"file:{database.as_posix()}?mode=ro",
        uri=True,
        timeout=60,
    )
    try:
        scans = connection.execute(
            """
            SELECT parser_version, COUNT(*), MIN(completed_at), MAX(completed_at)
            FROM fact_machinery_disclosure_cache_scan
            WHERE asof_date = ?
            GROUP BY parser_version
            ORDER BY parser_version
            """,
            (args.asof,),
        ).fetchall()
        statuses = connection.execute(
            """
            SELECT availability_status, COUNT(*)
            FROM feature_financial_metric_availability
            WHERE model_family = 'machinery' AND asof_date = ?
            GROUP BY availability_status
            ORDER BY availability_status
            """,
            (args.asof,),
        ).fetchall()
        coverage = connection.execute(
            f"""
            SELECT metric_name,
                   SUM(CASE WHEN availability_status IN ({",".join("?" for _ in COVERED_STATUSES)})
                            THEN 1 ELSE 0 END) AS covered_count,
                   SUM(CASE WHEN availability_status NOT IN ({",".join("?" for _ in EXCLUDED_STATUSES)})
                            THEN 1 ELSE 0 END) AS applicable_count
            FROM feature_financial_metric_availability
            WHERE model_family = 'machinery' AND asof_date = ?
            GROUP BY metric_name
            ORDER BY metric_name
            """,
            (*COVERED_STATUSES, *EXCLUDED_STATUSES, args.asof),
        ).fetchall()
        latest_dates: dict[str, str] = {}
        for table in (
            "feature_financial",
            "feature_financial_metric_availability",
            "feature_machinery_scoring",
            "score_machinery",
        ):
            if table not in {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }:
                continue
            family_filter = (
                "WHERE model_family = 'machinery'"
                if "model_family" in table_columns(connection, table)
                else ""
            )
            row = connection.execute(
                f"""
                SELECT MAX(asof_date)
                FROM "{table}"
                {family_filter}
                """
            ).fetchone()
            latest_dates[table] = str(row[0] or "")
        payload: dict[str, Any] = {
            "asof_date": args.asof,
            "scan_versions": scans,
            "availability_statuses": statuses,
            "metric_coverage": [
                {
                    "metric_name": str(row[0]),
                    "covered_count": int(row[1]),
                    "applicable_count": int(row[2]),
                }
                for row in coverage
            ],
            "latest_dates": latest_dates,
        }
        if args.quick_check:
            payload["quick_check"] = connection.execute(
                "PRAGMA quick_check"
            ).fetchone()[0]
        if args.baseline:
            payload["defense_comparison"] = defense_comparison(
                connection,
                args.baseline,
            )
        print(json.dumps(payload, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
