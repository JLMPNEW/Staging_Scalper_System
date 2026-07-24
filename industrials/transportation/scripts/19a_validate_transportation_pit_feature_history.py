#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from industrials.core.config import cfg_get, family_config, load_yaml, resolve_path  # noqa: E402
from industrials.transportation.contracts import write_manifest  # noqa: E402
from industrials.transportation.financial_contract import load_metric_registry  # noqa: E402
from industrials.transportation.scripts._shared import DEFAULT_CONFIG, MODEL_FAMILY  # noqa: E402


VALID_STATUSES = {
    "REPORTED",
    "DERIVED",
    "PROXY",
    "NOT_APPLICABLE",
    "NOT_DISCLOSED",
    "DISCLOSED_UNPARSED",
    "PARSER_FAILURE",
}
SNAPSHOT_FILES = (
    "reporting_profiles.csv",
    "market_features.csv",
    "financial_features.csv",
    "metric_availability.csv",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and hash-freeze transportation point-in-time historical "
            "market, financial, and specialized feature snapshots."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--input-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    return parser.parse_args()


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ticker_set(
    connection: sqlite3.Connection,
    *,
    table: str,
    asof: str,
) -> set[str]:
    if table not in {
        "feature_market_technical",
        "feature_financial_statement",
        "feature_financial_metric_availability",
    }:
        raise ValueError(f"unsupported table={table}")
    return {
        str(row[0])
        for row in connection.execute(
            f"""
            SELECT DISTINCT ticker
            FROM {table}
            WHERE model_family=? AND asof_date=?
            """,
            (MODEL_FAMILY, asof),
        ).fetchall()
    }


def main() -> int:
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    family = family_config(config, MODEL_FAMILY)
    historical = family["historical_features"]
    financial = family["financial"]
    base_dir = config_path.parent
    db_path = (
        args.db.expanduser().resolve()
        if args.db
        else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    )
    input_csv = (
        args.input_csv.expanduser().resolve()
        if args.input_csv
        else resolve_path(historical["build_report_csv"], base_dir=base_dir)
    )
    output_json = (
        args.output_json.expanduser().resolve()
        if args.output_json
        else resolve_path(
            historical["validation_output_json"], base_dir=base_dir
        )
    )
    output_root = resolve_path(historical["output_root"], base_dir=base_dir)
    build_manifest_path = resolve_path(
        historical["build_manifest_json"], base_dir=base_dir
    )
    build_manifest = read_json(build_manifest_path)
    registry_path = resolve_path(financial["metric_registry"], base_dir=base_dir)
    registry_version, metrics = load_metric_registry(registry_path)
    metric_count = len(metrics)
    report = read_rows(input_csv)
    errors: list[str] = []
    if build_manifest.get("acceptance") != "PASS":
        errors.append("historical feature build manifest does not PASS")
    if not report:
        errors.append("historical feature build report is empty")
    if any(str(row.get("status") or "") != "PASS" for row in report):
        failed = [
            str(row.get("asof_date") or "")
            for row in report
            if str(row.get("status") or "") != "PASS"
        ]
        errors.append(f"historical feature build contains failed dates={failed}")
    dates = sorted(str(row.get("asof_date") or "") for row in report)
    if len(dates) != len(set(dates)) or any(not value for value in dates):
        errors.append("historical feature build dates must be unique and nonblank")
    manifest_dates = sorted(
        str(value) for value in build_manifest.get("completed_dates", [])
    )
    if manifest_dates != dates:
        errors.append("build report dates do not match build manifest completed dates")
    cadence = str(build_manifest.get("observation_cadence") or "")
    full_scope = cadence != "explicit_dates"
    hashes: dict[str, dict[str, str]] = {}
    total_expected_rows = 0
    total_metric_rows = 0
    with read_only_connection(db_path) as connection:
        for asof in dates:
            expected = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT ticker
                    FROM dim_universe_membership
                    WHERE model_family=?
                      AND start_date<=?
                      AND COALESCE(end_date, '9999-12-31')>=?
                    """,
                    (MODEL_FAMILY, asof, asof),
                ).fetchall()
            }
            total_expected_rows += len(expected)
            market = ticker_set(
                connection,
                table="feature_market_technical",
                asof=asof,
            )
            financial_tickers = ticker_set(
                connection,
                table="feature_financial_statement",
                asof=asof,
            )
            availability = ticker_set(
                connection,
                table="feature_financial_metric_availability",
                asof=asof,
            )
            for label, actual in (
                ("market", market),
                ("financial", financial_tickers),
                ("availability", availability),
            ):
                if actual != expected:
                    errors.append(
                        f"{asof}:{label}: membership mismatch "
                        f"missing={sorted(expected-actual)[:10]} "
                        f"extra={sorted(actual-expected)[:10]}"
                    )
            metric_rows = connection.execute(
                """
                SELECT ticker, metric_name, availability_status, filing_date
                FROM feature_financial_metric_availability
                WHERE model_family=? AND asof_date=?
                """,
                (MODEL_FAMILY, asof),
            ).fetchall()
            total_metric_rows += len(metric_rows)
            expected_metrics = len(expected) * metric_count
            if len(metric_rows) != expected_metrics:
                errors.append(
                    f"{asof}: metric rows={len(metric_rows)} expected={expected_metrics}"
                )
            bad_status = [
                f"{row['ticker']}:{row['metric_name']}:{row['availability_status']}"
                for row in metric_rows
                if str(row["availability_status"]) not in VALID_STATUSES
            ]
            if bad_status:
                errors.append(f"{asof}: invalid availability statuses={bad_status[:10]}")
            future_metric = [
                f"{row['ticker']}:{row['metric_name']}:{row['filing_date']}"
                for row in metric_rows
                if str(row["filing_date"] or "")[:10] > asof
            ]
            if future_metric:
                errors.append(f"{asof}: future metric provenance={future_metric[:10]}")
            future_market = [
                f"{row['ticker']}:{row['latest_bar_date']}"
                for row in connection.execute(
                    """
                    SELECT ticker, latest_bar_date
                    FROM feature_market_technical
                    WHERE model_family=? AND asof_date=?
                      AND COALESCE(latest_bar_date, '')>?
                    """,
                    (MODEL_FAMILY, asof, asof),
                ).fetchall()
            ]
            if future_market:
                errors.append(f"{asof}: future market bars={future_market[:10]}")
            future_financial = [
                f"{row['ticker']}:{row['accession_number']}"
                for row in connection.execute(
                    """
                    SELECT feature.ticker, feature.accession_number
                    FROM feature_financial_statement AS feature
                    JOIN fact_sec_filing AS filing
                      ON filing.ticker=feature.ticker
                     AND filing.accession_number=feature.accession_number
                    WHERE feature.model_family=? AND feature.asof_date=?
                      AND COALESCE(
                        NULLIF(SUBSTR(filing.accepted_at, 1, 10), ''),
                        filing.filing_date
                      )>?
                    """,
                    (MODEL_FAMILY, asof, asof),
                ).fetchall()
            ]
            if future_financial:
                errors.append(
                    f"{asof}: future financial accessions={future_financial[:10]}"
                )
            snapshot_dir = output_root / asof
            hashes[asof] = {}
            for name in SNAPSHOT_FILES:
                path = snapshot_dir / name
                if not path.exists():
                    errors.append(f"{asof}: missing snapshot file={path}")
                    continue
                hashes[asof][name] = sha256(path)

    result: dict[str, Any] = {
        "acceptance": "PASS" if not errors else "FAIL",
        "model_family": MODEL_FAMILY,
        "panel_status": (
            "FROZEN"
            if not errors and full_scope
            else "PILOT_VALIDATED"
            if not errors
            else "NOT_FROZEN"
        ),
        "observation_cadence": cadence,
        "registry_version": registry_version,
        "metric_count": metric_count,
        "snapshot_date_count": len(dates),
        "first_snapshot_date": dates[0] if dates else "",
        "last_snapshot_date": dates[-1] if dates else "",
        "total_membership_rows": total_expected_rows,
        "total_metric_availability_rows": total_metric_rows,
        "snapshot_sha256": hashes,
        "build_report_csv": str(input_csv),
        "errors": errors[:100],
    }
    write_manifest(output_json, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
