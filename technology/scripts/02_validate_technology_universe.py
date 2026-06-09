#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from technology.core.config import cfg_get, load_yaml, resolve_path  # noqa: E402
from technology.core.db import connect, init_db  # noqa: E402
from technology.core.logging_utils import configure_utc_logging  # noqa: E402
from technology.core.text_norm import normalize_cik, normalize_ticker  # noqa: E402


LOGGER = logging.getLogger("validate_technology_universe")
DEFAULT_CONFIG = PACKAGE_ROOT / "config.yaml"
LOAD_STAGE = "technology_universe_load"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate the loaded technology universe.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--universe-csv", type=Path, default=None)
    parser.add_argument("--cohorts", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=None)
    parser.add_argument("--model-family", default="")
    return parser.parse_args()


def read_csv_flexible(path: Path) -> list[dict[str, str]]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames is None:
                    raise ValueError(f"CSV has no header: {path}")
                return [{str(key): str(value or "") for key, value in row.items()} for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    raise ValueError(f"Could not decode CSV {path}: {last_error}")


def row_get(row: dict[str, str], *keys: str) -> str:
    lowered = {str(key).strip().lower(): str(value or "") for key, value in row.items()}
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def load_yaml_map(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load Stage 2 policy YAML.") from exc
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_cohort_tickers(path: Path) -> dict[str, str]:
    data = load_yaml_map(path)
    cohorts = data.get("cohorts")
    if not isinstance(cohorts, list):
        raise ValueError(f"{path} must contain a cohorts list.")
    out: dict[str, str] = {}
    for raw in cohorts:
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid cohort mapping in {path}")
        cohort_id = str(raw.get("cohort_id") or "").strip()
        for raw_ticker in raw.get("tickers") or []:
            ticker = normalize_ticker(raw_ticker)
            if not ticker:
                continue
            if ticker in out:
                raise ValueError(f"Ticker {ticker} appears in multiple cohorts.")
            out[ticker] = cohort_id
    return out


def csv_tickers_and_rows(rows: list[dict[str, str]]) -> tuple[list[str], list[str]]:
    tickers: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()
    for row in rows:
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "symbol", "Symbol"))
        if not ticker:
            continue
        if ticker in seen:
            duplicates.append(ticker)
        seen.add(ticker)
        tickers.append(ticker)
    return tickers, duplicates


def scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row is not None else 0


def quoted_placeholders(values: list[str]) -> str:
    if not values:
        raise ValueError("values cannot be empty")
    return ",".join("?" for _ in values)


def validate() -> int:
    configure_utc_logging()
    args = parse_args()
    config_path = args.config.expanduser().resolve()
    config = load_yaml(config_path)
    base_dir = config_path.parent
    db_path = args.db.expanduser().resolve() if args.db else resolve_path(cfg_get(config, "paths.database_path"), base_dir=base_dir)
    universe_csv = (
        args.universe_csv.expanduser().resolve()
        if args.universe_csv
        else resolve_path(cfg_get(config, "technology_universe.seed_csv"), base_dir=base_dir)
    )
    cohort_path = (
        args.cohorts.expanduser().resolve()
        if args.cohorts
        else resolve_path(cfg_get(config, "technology_universe.cohort_path"), base_dir=base_dir)
    )
    policy_path = (
        args.policy.expanduser().resolve()
        if args.policy
        else resolve_path(cfg_get(config, "technology_universe.policy_path"), base_dir=base_dir)
    )
    model_family = str(args.model_family or cfg_get(config, "technology_universe.initial_subsector", "semiconductors")).strip()
    policy = load_yaml_map(policy_path)
    required_non_cik = [str(x) for x in policy.get("required_non_cik_fields", [])]
    expected_ticker_count = int(policy.get("expected_ticker_count") or 0)
    cohort_map = load_cohort_tickers(cohort_path)
    csv_rows = read_csv_flexible(universe_csv)
    tickers, duplicates = csv_tickers_and_rows(csv_rows)
    unique_tickers = sorted(set(tickers))
    ticker_params = tuple(unique_tickers)
    placeholders = quoted_placeholders(unique_tickers)

    errors: list[str] = []
    warnings: list[str] = []
    if duplicates:
        errors.append(f"Duplicate CSV tickers: {sorted(set(duplicates))}")
    if expected_ticker_count > 0 and len(unique_tickers) != expected_ticker_count:
        errors.append(
            f"Source-of-truth ticker count mismatch: expected={expected_ticker_count} actual={len(unique_tickers)}"
        )
    raw_csv_text = universe_csv.read_text(encoding="utf-8-sig", errors="replace")
    raw_contains_na_ticker = any(line.startswith("NA,") for line in raw_csv_text.splitlines())
    if raw_contains_na_ticker and "NA" not in set(unique_tickers):
        errors.append("CSV contains ticker NA, but parsing dropped it.")

    csv_required_missing = []
    for row in csv_rows:
        ticker = normalize_ticker(row_get(row, "ticker", "Ticker", "symbol", "Symbol"))
        for field in required_non_cik:
            if field == "ticker":
                continue
            if not row_get(row, field, field.title(), field.replace("_", " ")):
                csv_required_missing.append(f"{ticker}:{field}")
    if csv_required_missing:
        errors.append(f"CSV missing required non-CIK fields: {csv_required_missing[:20]}")

    cohort_missing_from_csv = sorted(set(cohort_map).difference(unique_tickers))
    if cohort_missing_from_csv:
        errors.append(f"Cohort tickers missing from CSV: {cohort_missing_from_csv}")

    with connect(db_path, timeout_sec=float(cfg_get(config, "runtime.sqlite_timeout_sec", 30.0))) as conn:
        init_db(conn)
        company_count = scalar(conn, f"SELECT COUNT(*) FROM dim_company WHERE ticker IN ({placeholders})", ticker_params)
        security_count = scalar(conn, f"SELECT COUNT(*) FROM dim_security WHERE ticker IN ({placeholders})", ticker_params)
        taxonomy_count = scalar(
            conn,
            f"SELECT COUNT(*) FROM dim_technology_taxonomy WHERE model_family = ? AND ticker IN ({placeholders})",
            (model_family, *ticker_params),
        )
        if company_count != len(unique_tickers):
            errors.append(f"dim_company count mismatch: db={company_count} csv_unique={len(unique_tickers)}")
        if security_count != len(unique_tickers):
            errors.append(f"dim_security count mismatch: db={security_count} csv_unique={len(unique_tickers)}")
        if taxonomy_count != len(unique_tickers):
            errors.append(f"dim_technology_taxonomy count mismatch: db={taxonomy_count} csv_unique={len(unique_tickers)}")

        if raw_contains_na_ticker:
            db_na = scalar(conn, "SELECT COUNT(*) FROM dim_company WHERE ticker = 'NA'")
            if db_na != 1:
                errors.append(f"Ticker NA should have exactly one dim_company row; found {db_na}")

        db_missing_required = conn.execute(
            f"""
            SELECT ticker FROM dim_company
            WHERE ticker IN ({placeholders})
              AND (
                company_name = '' OR sector = '' OR industry = '' OR subsector = ''
                OR country = '' OR currency = ''
              )
            ORDER BY ticker
            """,
            ticker_params,
        ).fetchall()
        if db_missing_required:
            errors.append(f"dim_company required fields missing for {[row[0] for row in db_missing_required]}")

        db_missing_security = conn.execute(
            f"""
            SELECT ticker FROM dim_security
            WHERE ticker IN ({placeholders})
              AND (exchange = '' OR security_type = '' OR listing_status = '' OR currency = '')
            ORDER BY ticker
            """,
            ticker_params,
        ).fetchall()
        if db_missing_security:
            errors.append(f"dim_security required fields missing for {[row[0] for row in db_missing_security]}")

        cohort_rows = conn.execute(
            f"""
            SELECT ticker, calibration_cohort_id FROM dim_technology_taxonomy
            WHERE model_family = ? AND ticker IN ({placeholders})
            """,
            (model_family, *ticker_params),
        ).fetchall()
        db_cohort_map = {str(row[0]): str(row[1] or "") for row in cohort_rows}
        bad_cohort_assignments = [
            f"{ticker}:expected={cohort_id}:actual={db_cohort_map.get(ticker)}"
            for ticker, cohort_id in cohort_map.items()
            if db_cohort_map.get(ticker) != cohort_id
        ]
        if bad_cohort_assignments:
            errors.append(f"Core cohort assignment mismatches: {bad_cohort_assignments[:20]}")

        unassigned_count = sum(1 for ticker in unique_tickers if db_cohort_map.get(ticker) == "semi_unassigned")
        missing_cik_count = scalar(conn, f"SELECT COUNT(*) FROM dim_company WHERE ticker IN ({placeholders}) AND COALESCE(cik, '') = ''", ticker_params)
        missing_cik_issue_count = scalar(
            conn,
            f"""
            SELECT COUNT(*) FROM data_quality_issues
            WHERE stage = ? AND issue_type = 'missing_cik' AND ticker IN ({placeholders})
            """,
            (LOAD_STAGE, *ticker_params),
        )
        unassigned_issue_count = scalar(
            conn,
            f"""
            SELECT COUNT(*) FROM data_quality_issues
            WHERE stage = ? AND issue_type = 'unassigned_semiconductor_cohort' AND ticker IN ({placeholders})
            """,
            (LOAD_STAGE, *ticker_params),
        )
        if missing_cik_issue_count != missing_cik_count:
            errors.append(f"missing_cik issue mismatch: issues={missing_cik_issue_count} missing_cik={missing_cik_count}")
        if unassigned_issue_count != unassigned_count:
            errors.append(f"unassigned cohort issue mismatch: issues={unassigned_issue_count} unassigned={unassigned_count}")

        csv_missing_cik = sum(1 for row in csv_rows if not normalize_cik(row_get(row, "cik", "CIK")))
        if missing_cik_count != csv_missing_cik:
            errors.append(f"DB missing CIK count {missing_cik_count} does not match CSV missing CIK count {csv_missing_cik}")

        warnings.append(f"CSV rows={len(csv_rows)} unique_tickers={len(unique_tickers)}")
        warnings.append(f"Core cohort tickers={len(cohort_map)}")
        warnings.append(f"Unassigned cohort rows={unassigned_count}")
        warnings.append(f"Missing CIK rows={missing_cik_count}")
        warnings.append(f"Data-quality issues: missing_cik={missing_cik_issue_count} unassigned={unassigned_issue_count}")

    for message in warnings:
        LOGGER.info(message)
    if errors:
        for message in errors:
            LOGGER.error(message)
        return 1
    LOGGER.info("Technology universe validation passed for model_family=%s", model_family)
    return 0


if __name__ == "__main__":
    raise SystemExit(validate())
